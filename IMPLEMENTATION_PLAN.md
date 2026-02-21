# Implementation Plan: Fix Permissions + Git Checkpoint/Rollback UX

## Overview

This plan addresses two critical improvements for Leonardo/LlamaBot:

1. **Fix UID/GID permission issues permanently** - Eliminate the need for manual `chown` commands and prevent `.git/index.lock` errors
2. **Implement Git-based checkpoint/rollback system** - Give users a clean UX to accept/reject AI agent changes

---

## PART 1: Fix Permissions Permanently

### Critical Understanding

**Root Cause**: Multiple containers write to shared volumes with mismatched UIDs:
- **LlamaBot** (Container 2): Runs as root, mounts `./:/app/leonardo` and `./rails:/app/app/rails`
- **LlamaPress** (Container 1): Runs as root (dev) or UID 1000 (prod), scaffolds/generates files
- **VSCode** (Container 3): Runs as UID 1000 (PUID=1000), can't modify root-owned files
- **Host**: Ubuntu user is UID 1000

**File Write Sources**:
1. LlamaBot's `bash_rails` tool → Docker exec into LlamaPress → Files owned by LlamaPress container user
2. LlamaBot's native file tools (`write_file`, `edit_file`) → Files owned by LlamaBot container user
3. LlamaBot's git operations → Files in `.git/` owned by LlamaBot container user
4. Direct Rails operations (scaffolds, console) → Files owned by LlamaPress container user
5. VSCode edits → Files owned by UID 1000

### TODO List 1: Permission Fixes

#### P0 - Audit Current State (One-Time)

**Purpose**: Confirm the hypothesis before making changes

**Tasks**:
- [ ] On a running instance, execute these commands and document output:
  ```bash
  docker compose exec llamapress id
  docker compose exec llamabot id
  docker compose exec code id
  ls -ln /home/ubuntu/Leonardo | head -20
  ls -ln /home/ubuntu/Leonardo/.git | head -20
  ls -ln /home/ubuntu/Leonardo/rails | head -20
  ```
- [ ] Test each write path and verify ownership:
  - Create a file via VSCode (manual edit)
  - Create a file via LlamaBot's native `write_file` tool (not bash_rails)
  - Run a Rails scaffold via LlamaBot's `bash_rails` tool
  - Run a direct Rails generator inside llamapress container
  - Run a git operation from LlamaBot
- [ ] Document which files are owned by which UID/GID
- [ ] Confirm expected final state: all files should be `1000:1000`

**Files to inspect**: None (command-line audit only)

---

#### P1 - Fix bash_rails Docker Exec User (HIGHEST PRIORITY)

**Location**: `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/app/agents/leonardo/rails_agent/tools.py`

**Why this matters**: The `bash_rails` tool is the primary source of file writes (scaffolds, generators, rake tasks). It currently executes commands in the Rails container as whatever user that container runs as (likely root in dev).

**Changes**:
- [ ] Add `"User": "1000:1000"` to the Docker exec API payload in `rails_api_sh()` function (around line 670)

**Before**:
```python
payload = {
    "AttachStdout": True,
    "AttachStderr": True,
    "Tty": True,
    "Cmd": ["/bin/sh", "-lc", snippet],
    "WorkingDir": workdir
}
```

**After**:
```python
payload = {
    "AttachStdout": True,
    "AttachStderr": True,
    "Tty": True,
    "Cmd": ["/bin/sh", "-lc", snippet],
    "WorkingDir": workdir,
    "User": "1000:1000"  # ← ADD THIS LINE
}
```

**Acceptance Criteria**:
- [ ] A Rails scaffold triggered via `bash_rails` creates files owned by `1000:1000`
- [ ] No new root-owned files appear in `/home/ubuntu/Leonardo/rails/`
- [ ] Rails generators, console commands, and rake tasks all create `1000:1000` files

**Files to modify**:
- `LlamaBot/app/agents/leonardo/rails_agent/tools.py` (line ~670)

---

#### P1 - Run LlamaBot Container as UID 1000

**Location**: `/Users/kodykendall/SoftEngineering/LLMPress/Leonardo/docker-compose-dev.yml` AND `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/docker-compose.yml`

**Why this matters**: LlamaBot has direct access to the full repo mount (`./:/app/leonardo`) including `.git/`. Any git operations it performs currently run as root, creating root-owned lock files that VSCode can't clean up.

**Changes**:
- [ ] Add `user: "1000:1000"` to the `llamabot` service in both docker-compose files
- [ ] Verify that `privileged: true` and `pid: host` still work (they should - these are capabilities, not UID-dependent)

**Before** (docker-compose-dev.yml):
```yaml
llamabot:
  build: ../LlamaBot
  env_file: .env
  pid: host
  privileged: true
  # ... rest of config
```

**After**:
```yaml
llamabot:
  build: ../LlamaBot
  env_file: .env
  user: "1000:1000"  # ← ADD THIS LINE
  pid: host
  privileged: true
  # ... rest of config
```

**Acceptance Criteria**:
- [ ] `docker compose exec llamabot id` shows `uid=1000 gid=1000`
- [ ] Any `.git/index.lock` files created by LlamaBot are owned by `1000:1000`
- [ ] LlamaBot can still execute `nsenter` commands (host access)
- [ ] LlamaBot can still access `/var/run/docker.sock`
- [ ] Native file writes from LlamaBot tools create `1000:1000` files

**Files to modify**:
- `Leonardo/docker-compose-dev.yml`
- `LlamaBot/docker-compose.yml`

---

#### P1 - Run LlamaPress as UID 1000 (Requires Image Changes)

**Location**:
- Dockerfile: `/Users/kodykendall/SoftEngineering/LLMPress/LlamaPress-Simple/Dockerfile`
- Docker Compose: `/Users/kodykendall/SoftEngineering/LLMPress/Leonardo/docker-compose-dev.yml`

**Why this matters**: Direct Rails operations (scaffolds, console, rake tasks) run as the Rails container user. If that's root, they create root-owned files.

**Changes to Dockerfile**:
- [ ] Add a non-root user at UID 1000 in the Dockerfile
- [ ] Ensure Rails runtime directories are writable by UID 1000
- [ ] Switch to running as that user

**Changes needed**:
```dockerfile
# Add near the end of Dockerfile, before CMD
RUN groupadd --system --gid 1000 rails && \
    useradd rails --uid 1000 --gid 1000 --create-home --shell /bin/bash && \
    chown -R rails:rails /rails/tmp /rails/log /rails/public /rails/db /rails/storage

USER 1000:1000

# Then existing CMD...
```

**Changes to docker-compose-dev.yml**:
- [ ] Add `user: "1000:1000"` to the `llamapress` service

```yaml
llamapress:
  build: ../LlamaPress-Simple
  user: "1000:1000"  # ← ADD THIS LINE
  env_file: .env
  # ... rest of config
```

**Acceptance Criteria**:
- [ ] `docker compose exec llamapress id` shows `uid=1000 gid=1000`
- [ ] `bundle exec rails db:prepare` completes successfully
- [ ] `bundle exec rails s` starts without permission errors
- [ ] Rails generators create files owned by `1000:1000`
- [ ] No root-owned files in `./rails/*` directories

**Files to modify**:
- `LlamaPress-Simple/Dockerfile`
- `Leonardo/docker-compose-dev.yml`

---

#### P1 - One-Time Cleanup Script for Existing Instances

**Purpose**: Fix existing root-owned files on already-deployed instances after code changes are applied

**Script to create**: `/home/ubuntu/Leonardo/scripts/fix-permissions.sh`

```bash
#!/bin/bash
# One-time permission repair script
# Run on host after deploying UID fixes

set -e

echo "🔧 Stopping containers to prevent concurrent writes..."
cd /home/ubuntu/Leonardo
docker compose down

echo "🧹 Removing stale git locks..."
find /home/ubuntu/Leonardo/.git -name "*.lock" -type f -delete 2>/dev/null || true

echo "👤 Fixing ownership to ubuntu user (UID 1000)..."
chown -R 1000:1000 /home/ubuntu/Leonardo

echo "✅ Setting proper permissions..."
# Directories: rwxrwxr-x
find /home/ubuntu/Leonardo -type d -exec chmod 775 {} \;
# Files: rw-rw-r--
find /home/ubuntu/Leonardo -type f -exec chmod 664 {} \;

echo "🚀 Restarting containers..."
docker compose up -d

echo "✅ Permission repair complete!"
```

**Tasks**:
- [ ] Create the script on all existing instances
- [ ] Make it executable: `chmod +x scripts/fix-permissions.sh`
- [ ] Run it once on each instance after deploying the code fixes
- [ ] Verify no permission errors remain after restart

**Acceptance Criteria**:
- [ ] All files in `/home/ubuntu/Leonardo` are owned by `1000:1000`
- [ ] No `.git/*.lock` files remain
- [ ] VSCode can discard changes without permission errors
- [ ] Git operations work from VSCode

---

#### P2 - Shared Group + Setgid Safety Net (Belt-and-Suspenders)

**Purpose**: Even if UID standardization works, a shared group prevents future breakage

**Tasks on Host**:
- [ ] Create a shared group: `sudo groupadd -g 1001 leonardo`
- [ ] Add ubuntu user to it: `sudo usermod -aG leonardo ubuntu`
- [ ] Set group ownership: `sudo chgrp -R leonardo /home/ubuntu/Leonardo`
- [ ] Set setgid bit on directories: `sudo find /home/ubuntu/Leonardo -type d -exec chmod g+s {} \;`
- [ ] Set group-writable permissions: `sudo chmod -R g+w /home/ubuntu/Leonardo`

**Changes to docker-compose** (optional, if needed):
```yaml
services:
  llamabot:
    user: "1000:1000"
    group_add:
      - "1001"  # leonardo group

  llamapress:
    user: "1000:1000"
    group_add:
      - "1001"

  code:
    environment:
      - PGID=1001  # Use leonardo group instead of 1000
```

**Why this helps**: If any process accidentally creates a file with a different UID, group write access still prevents lock-out.

**Acceptance Criteria**:
- [ ] New files created by any container inherit GID 1001
- [ ] All containers can write to shared directories
- [ ] Permission errors don't occur even if UID drifts

**Files to modify** (if using group_add approach):
- `Leonardo/docker-compose-dev.yml`
- `LlamaBot/docker-compose.yml`

---

#### P2 - Centralize Git Operations (Policy Change)

**Purpose**: Prevent multiple containers from fighting over `.git/` ownership

**Recommended Policy**:

**Option A (Best for Product UX)**:
- All Git operations for checkpoints happen through dedicated backend service (LlamaBot API endpoint)
- VSCode is optional/manual dev tooling, NOT the main rollback mechanism
- Users interact with "Versions" / "Checkpoints" UI, not raw git

**Option B (Hybrid)**:
- Allow VSCode git use for manual development
- LlamaBot should NOT run arbitrary git commands except through checkpoint service
- Checkpoint service is the source of truth for rollbacks

**Tasks**:
- [ ] Document which container "owns" git operations
- [ ] Remove or restrict direct git commands from other containers
- [ ] Ensure checkpoint system is the primary interface (see Part 2)

**Acceptance Criteria**:
- [ ] Clear separation of responsibilities
- [ ] `.git/` is only modified by one service at a time
- [ ] Reduced lock file conflicts

---

#### P2 - Add Repo Health Check Endpoint

**Location**: Create `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/app/routers/health.py`

**Purpose**: Quick diagnostics to catch permission issues before they cause problems

**New endpoint**:
```python
@router.get("/api/health/repo")
def check_repo_health():
    """Check repository health and permissions"""
    import os
    import subprocess

    leonardo_path = "/app/leonardo"
    git_path = f"{leonardo_path}/.git"

    # Check current process UID/GID
    uid = os.getuid()
    gid = os.getgid()

    # Check .git directory ownership
    git_stat = os.stat(git_path)
    git_uid = git_stat.st_uid
    git_gid = git_stat.st_gid

    # Check for lock files
    lock_files = subprocess.run(
        ["find", git_path, "-name", "*.lock"],
        capture_output=True,
        text=True
    )

    # Test git operations
    try:
        git_status = subprocess.run(
            ["git", "-C", leonardo_path, "status"],
            capture_output=True,
            text=True,
            timeout=5
        )
        git_works = git_status.returncode == 0
    except:
        git_works = False

    return {
        "process_uid": uid,
        "process_gid": gid,
        "git_directory_uid": git_uid,
        "git_directory_gid": git_gid,
        "lock_files": lock_files.stdout.splitlines(),
        "git_operational": git_works,
        "status": "healthy" if (uid == git_uid and len(lock_files.stdout) == 0 and git_works) else "degraded"
    }
```

**Tasks**:
- [ ] Create the health check endpoint
- [ ] Add route to main FastAPI app
- [ ] Create UI in frontend to display health status
- [ ] Add automatic health check on app startup

**Acceptance Criteria**:
- [ ] Endpoint returns accurate UID/GID information
- [ ] Lock files are detected
- [ ] Git operational status is correctly reported
- [ ] Health status shows in admin UI

**Files to create/modify**:
- Create: `LlamaBot/app/routers/health.py`
- Modify: `LlamaBot/app/main.py` (add router)

---

#### P3 - Automate Git Identity Setup on New EC2s

**Purpose**: Eliminate manual `git config` and `gh auth login` steps

**Changes to VSCode Container**:

Add environment variables to docker-compose:
```yaml
code:
  image: kody06/llamabot-vscode:0.2.0
  environment:
    - TZ=America/Denver
    - PASSWORD=password
    - PUID=1000
    - PGID=1000
    - GIT_USER_NAME=${GIT_USER_NAME}      # ← ADD
    - GIT_USER_EMAIL=${GIT_USER_EMAIL}    # ← ADD
    - GITHUB_TOKEN=${GITHUB_TOKEN}        # ← ADD
```

**Create entrypoint script** to auto-configure git (if VSCode image supports custom init):

`/config/custom-cont-init.d/99-git-setup.sh`:
```bash
#!/bin/bash
if [ -n "$GIT_USER_NAME" ] && [ -n "$GIT_USER_EMAIL" ]; then
    git config --global user.name "$GIT_USER_NAME"
    git config --global user.email "$GIT_USER_EMAIL"
    echo "✅ Git identity configured"
fi

if [ -n "$GITHUB_TOKEN" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token
    echo "✅ GitHub authenticated"
fi
```

**Update .env.example**:
```
GIT_USER_NAME=Your Name
GIT_USER_EMAIL=you@example.com
GITHUB_TOKEN=ghp_xxxxxxxxxxxxx
```

**Tasks**:
- [ ] Add environment variables to docker-compose files
- [ ] Create/verify VSCode entrypoint script support
- [ ] Update `.env.example` with new variables
- [ ] Document in AMI setup instructions
- [ ] Test on fresh EC2 instance

**Acceptance Criteria**:
- [ ] VSCode container auto-configures git on first start
- [ ] `gh auth status` shows authenticated in VSCode terminal
- [ ] No manual `git config` required
- [ ] Works on fresh EC2 instances from AMI

**Files to modify**:
- `Leonardo/docker-compose-dev.yml`
- `Leonardo/.env.example`
- Create VSCode init script (if supported by image)

---

#### P3 - Eliminate VSCode → Host SSH Requirement

**Purpose**: The SSH connection exists primarily to run `chown` fixes. Once permissions are solved, this should be unnecessary.

**Tasks**:
- [ ] Document why SSH access is currently needed
- [ ] After implementing permission fixes, test if SSH is still required
- [ ] If still needed, provide explicit admin tooling instead of full SSH
- [ ] Remove SSH key setup from onboarding docs if no longer needed

**Acceptance Criteria**:
- [ ] Users never need to SSH into host from VSCode
- [ ] All necessary admin operations available through UI or API
- [ ] Reduced security surface area

---

### Summary of Files to Modify (Part 1)

| File | Change |
|------|--------|
| `LlamaBot/app/agents/leonardo/rails_agent/tools.py` | Add `"User": "1000:1000"` to exec payload |
| `Leonardo/docker-compose-dev.yml` | Add `user: "1000:1000"` to llamabot and llamapress |
| `LlamaBot/docker-compose.yml` | Add `user: "1000:1000"` to llamabot |
| `LlamaPress-Simple/Dockerfile` | Add non-root user creation, chown, and USER directive |
| `LlamaBot/app/routers/health.py` | Create new health check endpoint |
| `LlamaBot/app/main.py` | Add health router |
| `Leonardo/scripts/fix-permissions.sh` | Create cleanup script |
| `Leonardo/.env.example` | Add git identity variables |

---

## PART 2: Git Checkpoint/Rollback System with UI/UX

### Critical Understanding

**What exists today**:
- LangGraph stores full conversation history in PostgreSQL via checkpointer
- ThreadMetadata table tracks conversation metadata (title, timestamps, message count)
- Git operations available via tools (`git_status`, `git_commit`, `git_command`)
- Frontend has modular architecture (ThreadManager, MessageRenderer, WebSocketManager, etc.)

**What's missing**:
- No user-facing checkpoint/rollback UI
- No way to preview changes before accepting
- No one-click "reject these changes" flow
- No visual diff or file change indicators in chat UI

**User Experience Goals** (from Darren's feedback):
- "I need a way to rollback/discard changes I don't like"
- "Too much work to discard via VSCode source control"
- "Should work even when file permissions fail"

### Architecture Decision: Git-Based Checkpoints

**Why Git (not tar)**:
- Git already tracks all changes
- Provides natural diff/blame/history
- Integrates with existing developer workflows
- Enables file-level granularity
- Already installed and working

**Checkpoint Strategy**:
1. Before AI agent makes changes → Create checkpoint (git commit)
2. Agent makes changes → Files modified on disk
3. Show user diff/changed files → UI displays what changed
4. User decides → Accept (keep changes) or Reject (rollback)
5. If reject → `git reset --hard <checkpoint_id>`

### TODO List 2: Checkpoint/Rollback Implementation

#### Phase 1: Backend - Checkpoint Storage and Management

**Location**: Create `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/app/services/checkpoint_service.py`

**Purpose**: Centralized service for creating, listing, and restoring git-based checkpoints

**Tasks**:

- [ ] **Create CheckpointService class** with methods:
  - `create_checkpoint(thread_id: str, description: str) -> CheckpointInfo`
    - Run `git add .` on `/app/leonardo`
    - Create commit with standardized message format
    - Store checkpoint metadata (commit SHA, thread_id, timestamp, description)
    - Return checkpoint info

  - `list_checkpoints(thread_id: str) -> List[CheckpointInfo]`
    - Query git log for commits related to this thread
    - Filter by commit message format or thread_id tag
    - Return list of available checkpoints

  - `get_checkpoint_diff(checkpoint_id: str) -> Dict[str, Any]`
    - Run `git show <checkpoint_id> --stat` to get file changes
    - Run `git show <checkpoint_id>` to get full diff
    - Parse and return structured diff data

  - `rollback_to_checkpoint(checkpoint_id: str) -> bool`
    - Validate checkpoint exists
    - Run `git reset --hard <checkpoint_id>`
    - Clean untracked files if needed
    - Return success/failure

  - `get_changed_files(checkpoint_id: str) -> List[str]`
    - Return list of files modified since checkpoint
    - Use `git diff --name-only <checkpoint_id> HEAD`

**Data Model** (add to existing models or create new):
```python
class CheckpointInfo:
    checkpoint_id: str      # Git commit SHA
    thread_id: str          # Associated conversation
    created_at: datetime    # When checkpoint was created
    description: str        # Human-readable description
    changed_files: List[str]  # Files that will be affected by rollback
    commit_message: str     # Full git commit message
```

**Commit Message Format** (for easy parsing):
```
🔖 Checkpoint: {description}

Thread: {thread_id}
Agent: Leonardo
Timestamp: {iso_timestamp}
```

**Files to create**:
- `LlamaBot/app/services/checkpoint_service.py`
- `LlamaBot/app/models/checkpoint.py` (if using separate model file)

**Acceptance Criteria**:
- [ ] Can create checkpoint before agent edits
- [ ] Can list all checkpoints for a thread
- [ ] Can get diff/changed files for a checkpoint
- [ ] Can rollback to any checkpoint
- [ ] All operations work with UID 1000 (no permission errors)

---

#### Phase 2: Backend - API Endpoints

**Location**: Create `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/app/routers/checkpoints.py`

**Purpose**: REST API for frontend to interact with checkpoint system

**Endpoints to create**:

- [ ] **`POST /api/checkpoints`** - Create new checkpoint
  ```python
  @router.post("/api/checkpoints")
  def create_checkpoint(request: CreateCheckpointRequest):
      checkpoint = checkpoint_service.create_checkpoint(
          thread_id=request.thread_id,
          description=request.description
      )
      return checkpoint
  ```

- [ ] **`GET /api/checkpoints?thread_id={id}`** - List checkpoints for thread
  ```python
  @router.get("/api/checkpoints")
  def list_checkpoints(thread_id: str):
      checkpoints = checkpoint_service.list_checkpoints(thread_id)
      return {"checkpoints": checkpoints}
  ```

- [ ] **`GET /api/checkpoints/{checkpoint_id}/diff`** - Get diff for checkpoint
  ```python
  @router.get("/api/checkpoints/{checkpoint_id}/diff")
  def get_checkpoint_diff(checkpoint_id: str):
      diff_data = checkpoint_service.get_checkpoint_diff(checkpoint_id)
      return diff_data
  ```

- [ ] **`POST /api/checkpoints/{checkpoint_id}/rollback`** - Rollback to checkpoint
  ```python
  @router.post("/api/checkpoints/{checkpoint_id}/rollback")
  def rollback_checkpoint(checkpoint_id: str):
      success = checkpoint_service.rollback_to_checkpoint(checkpoint_id)
      return {"success": success}
  ```

- [ ] **`GET /api/checkpoints/current-changes`** - Get uncommitted changes
  ```python
  @router.get("/api/checkpoints/current-changes")
  def get_current_changes():
      # Run git status to show what would be in next checkpoint
      changed_files = checkpoint_service.get_changed_files()
      return {"changed_files": changed_files}
  ```

**Tasks**:
- [ ] Create router file
- [ ] Add router to main FastAPI app
- [ ] Add authentication/authorization (verify user owns thread)
- [ ] Add error handling for git failures
- [ ] Add validation for checkpoint_id format

**Files to create/modify**:
- Create: `LlamaBot/app/routers/checkpoints.py`
- Modify: `LlamaBot/app/main.py` (add router)

**Acceptance Criteria**:
- [ ] All endpoints return correct data
- [ ] Error handling works (invalid checkpoint_id, git errors, etc.)
- [ ] Authentication prevents unauthorized access
- [ ] Works from frontend (verified with Postman or curl)

---

#### Phase 3: Backend - Auto-Checkpoint on Agent Edits

**Location**: Modify `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/app/agents/leonardo/rails_agent/tools.py`

**Purpose**: Automatically create checkpoints before agent makes file changes

**Strategy**: Wrap file modification tools to auto-checkpoint

**Tasks**:

- [ ] **Add checkpoint hook to `write_file` tool** (around line 169)
  - Before writing, check if this is a new edit session
  - If yes, create checkpoint with description like "Before AI edit: {filename}"
  - Then proceed with write

- [ ] **Add checkpoint hook to `edit_file` tool** (around line 214)
  - Same logic as write_file

- [ ] **Add checkpoint hook to `bash_command` tool** (around line 779)
  - Only checkpoint if command modifies files (e.g., `rails g`, `rake db:migrate`)
  - Skip for read-only commands

- [ ] **Track "edit session" to avoid duplicate checkpoints**
  - Use thread-local storage or context var
  - One checkpoint per agent turn (not per file)
  - Example: "Before agent turn #42"

**Implementation Example**:
```python
def maybe_create_checkpoint(runtime: ToolRuntime, description: str):
    """Create checkpoint if this is a new edit session"""
    thread_id = runtime.thread_id

    # Check if we already checkpointed this turn
    if hasattr(runtime, '_checkpointed') and runtime._checkpointed:
        return

    # Create checkpoint
    checkpoint_service.create_checkpoint(
        thread_id=thread_id,
        description=description
    )

    # Mark as checkpointed
    runtime._checkpointed = True

@tool(description=WRITE_FILE_DESCRIPTION)
def write_file(file_path: str, content: str, runtime: ToolRuntime) -> Command:
    # Auto-checkpoint before writing
    maybe_create_checkpoint(runtime, f"Before agent edit: {file_path}")

    # ... existing write logic
```

**Files to modify**:
- `LlamaBot/app/agents/leonardo/rails_agent/tools.py`

**Acceptance Criteria**:
- [ ] Checkpoint created before agent modifies files
- [ ] Only one checkpoint per agent turn (no duplicates)
- [ ] Checkpoint description is meaningful
- [ ] Doesn't break existing tool functionality

---

#### Phase 4: Frontend - Checkpoint UI Component

**Location**: Create `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/app/frontend/chat/checkpoints/CheckpointManager.js`

**Purpose**: UI component to display and interact with checkpoints

**Component Structure**:

```javascript
class CheckpointManager {
  constructor(chatApp) {
    this.chatApp = chatApp;
    this.currentThreadId = null;
    this.checkpoints = [];
    this.isVisible = false;
  }

  async fetchCheckpoints(threadId) {
    // GET /api/checkpoints?thread_id={threadId}
    // Update this.checkpoints
  }

  renderCheckpointList() {
    // Display checkpoints as timeline or list
    // Each checkpoint shows:
    //   - Description
    //   - Timestamp (relative: "2 hours ago")
    //   - Changed files count
    //   - Actions: View Diff, Rollback
  }

  async showDiff(checkpointId) {
    // GET /api/checkpoints/{checkpointId}/diff
    // Display diff in modal or side panel
  }

  async rollback(checkpointId) {
    // Show confirmation dialog
    // POST /api/checkpoints/{checkpointId}/rollback
    // Refresh UI on success
  }

  toggleVisibility() {
    // Show/hide checkpoint panel
  }
}
```

**UI Location**: Add checkpoint panel to sidebar (similar to thread list)

**Tasks**:

- [ ] Create CheckpointManager class
- [ ] Add checkpoint button to chat header
- [ ] Create checkpoint panel UI (HTML/CSS)
- [ ] Implement fetch/render logic
- [ ] Add diff viewer modal
- [ ] Add rollback confirmation dialog
- [ ] Integrate with ChatApp main class
- [ ] Add keyboard shortcuts (optional)

**Files to create/modify**:
- Create: `app/frontend/chat/checkpoints/CheckpointManager.js`
- Create: `app/frontend/chat/checkpoints/DiffViewer.js` (for diff display)
- Modify: `app/frontend/chat/index.js` (integrate CheckpointManager)
- Modify: `app/frontend/chat.html` (add checkpoint UI elements)
- Modify: `app/frontend/style.css` (checkpoint panel styles)

**Acceptance Criteria**:
- [ ] Checkpoint panel opens/closes smoothly
- [ ] Checkpoint list loads correctly
- [ ] Timestamps display as relative time
- [ ] Diff viewer shows changed files
- [ ] Rollback confirmation prevents accidents
- [ ] UI updates after rollback

---

#### Phase 5: Frontend - Changed Files Indicator

**Location**: Modify `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/app/frontend/chat/messages/MessageRenderer.js`

**Purpose**: Show which files were changed in each AI message

**Design**: Add file badges below AI messages that made edits

**Example**:
```
🦙 Leonardo: I've updated your User model to add email validation.

📄 app/models/user.rb (12 lines changed)
📄 spec/models/user_spec.rb (8 lines added)

[View Changes] [Rollback to Before This Edit]
```

**Tasks**:

- [ ] Parse tool calls from AI messages to extract file operations
- [ ] Render file change summary below AI message
- [ ] Add "View Changes" button → opens diff viewer
- [ ] Add "Rollback" button → rolls back to checkpoint before this message
- [ ] Style file badges consistently
- [ ] Handle multiple files changed in one message

**Files to modify**:
- `app/frontend/chat/messages/MessageRenderer.js`
- `app/frontend/style.css`

**Acceptance Criteria**:
- [ ] File changes display correctly below AI messages
- [ ] Click "View Changes" shows diff
- [ ] Click "Rollback" restores to before edit
- [ ] Works for single and multiple file edits
- [ ] Doesn't break existing message rendering

---

#### Phase 6: Frontend - Accept/Reject Flow (Proactive Prompts)

**Location**: Enhance checkpoint UI with proactive accept/reject prompts

**Design**: After AI makes changes, show prominent accept/reject buttons

**Flow**:
1. AI agent makes edits
2. Auto-checkpoint created before edits
3. UI shows: "Leonardo made changes to 3 files. Accept or Reject?"
4. User clicks **Accept** → Mark as accepted, continue
5. User clicks **Reject** → Rollback to pre-edit checkpoint, show confirmation

**UI Mockup**:
```
┌─────────────────────────────────────────┐
│ ⚠️  Leonardo edited 3 files             │
│                                         │
│ 📄 app/models/user.rb                   │
│ 📄 app/controllers/users_controller.rb  │
│ 📄 spec/models/user_spec.rb             │
│                                         │
│ [View Changes]  [✓ Accept]  [✗ Reject] │
└─────────────────────────────────────────┘
```

**Tasks**:

- [ ] Detect when agent has finished editing (message complete)
- [ ] Show accept/reject prompt automatically
- [ ] "Accept" button → Clear prompt, mark checkpoint as accepted
- [ ] "Reject" button → Show confirmation, rollback, show success message
- [ ] Handle rapid accept/reject actions (debouncing)
- [ ] Persist accept/reject state in ThreadMetadata

**Files to modify**:
- `app/frontend/chat/checkpoints/CheckpointManager.js`
- `app/frontend/chat/messages/MessageRenderer.js`
- `app/frontend/chat.html`
- `app/frontend/style.css`

**Acceptance Criteria**:
- [ ] Prompt appears after agent edits
- [ ] Accept clears prompt and continues
- [ ] Reject rolls back changes successfully
- [ ] Confirmation prevents accidental rollbacks
- [ ] UI state updates correctly

---

#### Phase 7: Frontend - Checkpoint Timeline View

**Location**: Create visual timeline of checkpoints in sidebar

**Design**: Show checkpoints as a branching timeline (similar to Git graph)

**Features**:
- Chronological list with timestamps
- Visual indicator for current position
- Branch visualization if user rolls back and makes new changes
- Hover shows changed files
- Click shows diff

**Tasks**:

- [ ] Design timeline UI component
- [ ] Render checkpoints as timeline items
- [ ] Add current position indicator
- [ ] Implement hover tooltips
- [ ] Add click to view diff
- [ ] Style with CSS (use timeline library or custom)
- [ ] Handle long lists (virtualization or pagination)

**Files to create/modify**:
- Create: `app/frontend/chat/checkpoints/CheckpointTimeline.js`
- Modify: `app/frontend/chat.html`
- Modify: `app/frontend/style.css`

**Acceptance Criteria**:
- [ ] Timeline displays correctly
- [ ] Current position is clear
- [ ] Hover shows file info
- [ ] Click opens diff viewer
- [ ] Performance is good with many checkpoints

---

#### Phase 8: Polish and Edge Cases

**Tasks**:

- [ ] **Handle rollback failures gracefully**
  - Show error message if git rollback fails
  - Suggest manual recovery steps
  - Log errors for debugging

- [ ] **Add loading states**
  - Show spinner while creating checkpoint
  - Show spinner while rolling back
  - Disable buttons during operations

- [ ] **Add success/error notifications**
  - Toast notification on successful rollback
  - Error notification on failure
  - Clear messaging

- [ ] **Handle concurrent edits**
  - Warn user if files changed outside of Leonardo
  - Offer to create checkpoint before rollback
  - Prevent data loss

- [ ] **Cleanup old checkpoints**
  - Add auto-cleanup for checkpoints older than X days
  - Add manual "Delete Checkpoint" button
  - Confirm before deleting

- [ ] **Add keyboard shortcuts**
  - `Ctrl+Z` or `Cmd+Z` to rollback last change
  - `Ctrl+Shift+Z` to redo (if applicable)
  - Document shortcuts in help

- [ ] **Add to onboarding/help docs**
  - Explain checkpoint system to users
  - Show how to rollback changes
  - Best practices for using checkpoints

**Files to modify**: Various (based on specific edge case)

**Acceptance Criteria**:
- [ ] All edge cases handled gracefully
- [ ] Error messages are helpful
- [ ] Loading states prevent double-clicks
- [ ] Notifications are non-intrusive
- [ ] Keyboard shortcuts work
- [ ] Documentation is clear

---

### Summary of Files to Create/Modify (Part 2)

| File | Purpose |
|------|---------|
| `LlamaBot/app/services/checkpoint_service.py` | Core checkpoint logic |
| `LlamaBot/app/models/checkpoint.py` | Data models |
| `LlamaBot/app/routers/checkpoints.py` | API endpoints |
| `LlamaBot/app/agents/leonardo/rails_agent/tools.py` | Auto-checkpoint hooks |
| `app/frontend/chat/checkpoints/CheckpointManager.js` | Frontend checkpoint UI |
| `app/frontend/chat/checkpoints/DiffViewer.js` | Diff display component |
| `app/frontend/chat/checkpoints/CheckpointTimeline.js` | Timeline visualization |
| `app/frontend/chat/messages/MessageRenderer.js` | File change indicators |
| `app/frontend/chat/index.js` | Integrate CheckpointManager |
| `app/frontend/chat.html` | UI structure |
| `app/frontend/style.css` | Styling |

---

## Testing Plan

### Part 1 (Permissions)
- [ ] Fresh EC2 from AMI - verify no manual chown needed
- [ ] Rails scaffold - verify files owned by 1000:1000
- [ ] LlamaBot file write - verify ownership
- [ ] Git operations - verify no .git/index.lock errors
- [ ] VSCode discard changes - verify no permission errors

### Part 2 (Checkpoints)
- [ ] Create checkpoint - verify git commit created
- [ ] List checkpoints - verify all checkpoints returned
- [ ] View diff - verify diff displays correctly
- [ ] Rollback - verify files restored correctly
- [ ] Accept/reject flow - verify UI updates correctly
- [ ] Auto-checkpoint - verify checkpoint created before edits
- [ ] Timeline - verify visual timeline renders
- [ ] Edge cases - verify error handling works

---

## Implementation Order Recommendation

### Week 1: Critical Permission Fixes
1. P0 - Audit current state
2. P1 - Fix bash_rails exec user
3. P1 - Run LlamaBot as UID 1000
4. P1 - Run LlamaPress as UID 1000
5. P1 - One-time cleanup script

### Week 2: Checkpoint Backend
1. Phase 1 - CheckpointService
2. Phase 2 - API endpoints
3. Phase 3 - Auto-checkpoint hooks
4. Test backend thoroughly

### Week 3: Checkpoint Frontend
1. Phase 4 - CheckpointManager UI
2. Phase 5 - Changed files indicator
3. Phase 6 - Accept/reject flow
4. Test UI thoroughly

### Week 4: Polish and Deploy
1. Phase 7 - Timeline view
2. Phase 8 - Edge cases and polish
3. P2 - Shared group safety net
4. P2/P3 - Additional permission improvements
5. Full integration testing
6. Deploy to production

---

## Critical Files Reference

### Permission Fixes
- `LlamaBot/app/agents/leonardo/rails_agent/tools.py` - bash_rails user fix
- `Leonardo/docker-compose-dev.yml` - UID configuration
- `LlamaPress-Simple/Dockerfile` - Non-root user creation

### Checkpoint System
- `LlamaBot/app/services/checkpoint_service.py` - Core logic
- `LlamaBot/app/routers/checkpoints.py` - API
- `app/frontend/chat/checkpoints/CheckpointManager.js` - Frontend UI
- `app/frontend/chat/messages/MessageRenderer.js` - File change display

---

## Success Criteria

### Part 1 Success:
- ✅ No manual `chown` commands needed
- ✅ No `.git/index.lock` errors
- ✅ VSCode discard works reliably
- ✅ All containers write files as UID 1000
- ✅ Fresh EC2 instances work without manual fixes

### Part 2 Success:
- ✅ Users can preview AI changes before accepting
- ✅ One-click rollback works reliably
- ✅ File change indicators show what changed
- ✅ Timeline shows checkpoint history
- ✅ No permission errors during rollback
- ✅ Darren can confidently reject unwanted changes

---

## Notes

**Permission fixes must be completed BEFORE checkpoint system**, otherwise rollback will fail due to permission issues.

The checkpoint system builds on top of the permission fixes to provide a seamless, reliable rollback experience.

Both parts are essential for meeting Darren's feedback: "I need a way to rollback/discard changes I don't like" without permission headaches.
