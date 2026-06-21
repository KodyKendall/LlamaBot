# GitHub Repo Auto-Provisioning

**Status:** Planned  
**Goal:** Eliminate the manual admin step of creating a GitHub repo, setting the origin remote, and adding users as collaborators when a new LlamaPress instance is provisioned.

---

## Background

LlamaBot already has GitHub Device Flow OAuth wired up (`app/routers/github_auth.py`). When a user authenticates, their personal GitHub token is installed on the host via `gh auth login`. This lets them push code to repos they already have access to — but it does nothing to *create* those repos or wire up the remote.

Currently a LlamaPress admin must manually:
1. Create a repo in the `llamapress-ai` GitHub org
2. Add the user as a collaborator
3. SSH into the instance and run `git remote set-url origin ...`

This plan automates all three steps.

---

## How GitHub Apps work (relevant background)

The `leonardo-from-llamapress` GitHub App supports two distinct token types:

| Token type | How obtained | What it can do |
|---|---|---|
| **User-to-server** | Device Flow OAuth (already wired up) | Acts as the authenticated user |
| **App installation token** | JWT signed with App private key → exchanged for short-lived token | Acts as the App on the org — create repos, add collaborators |

The installation token is the key. It requires no user action and has org-level authority.

---

## New environment variables required

All three values come from https://github.com/settings/apps/leonardo-from-llamapress:

```
GITHUB_APP_ID=                  # "App ID" at the top of the settings page
GITHUB_APP_PRIVATE_KEY=         # PEM contents from "Private keys" section (download .pem, paste value)
GITHUB_APP_INSTALLATION_ID=     # Install App → llamapress-ai org → numeric ID in the URL
```

Add these to `Leonardo/.env` and `.env.example` with comments.

---

## GitHub App permissions to verify

In the App's "Permissions & events" settings, confirm:

- `Repository permissions → Administration: Read & Write` — needed to create repos
- `Repository permissions → Contents: Read & Write` — already needed for push
- `Repository permissions → Metadata: Read` — required baseline
- `Organization permissions → Members: Read & Write` — needed to add collaborators

---

## Implementation plan

### 1. `app/agents/leonardo/github_app.py` (new file)

A small module for generating installation tokens. No side effects — pure token generation.

```python
def get_installation_token() -> str:
    """
    1. Read GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, GITHUB_APP_INSTALLATION_ID from env
    2. Build a JWT (10-min expiry) signed with the private key using PyJWT (RS256)
    3. POST https://api.github.com/app/installations/{id}/access_tokens
    4. Return the short-lived token string
    """
```

Dependencies: `PyJWT` (already in most Python envs; add to `pyproject.toml` if missing), `httpx` (already present).

---

### 2. `app/routers/github_auth.py` — extend `poll_device_auth`

After a successful device flow exchange, we already have the user's access token. Add two steps:

```python
# After getting access_token from GitHub:
github_username = await _get_github_username(access_token)   # GET /user
await _provision_repo_if_needed(github_username)              # see step 3
```

`_get_github_username(token)` — one `GET https://api.github.com/user` call, return `login` field.

---

### 3. `app/routers/github_auth.py` — `_provision_repo_if_needed`

Or alternatively a standalone endpoint `POST /api/github/provision-repo` so it can be called independently (e.g. from an admin panel).

Steps:
1. Check if a repo already exists for this instance (read `GITHUB_REPO_NAME` from env, or derive from instance slug). If it already exists, skip creation.
2. **Create the repo** — `POST https://api.github.com/orgs/llamapress-ai/repos` using the installation token.
3. **Add the user as collaborator** — `PUT https://api.github.com/repos/llamapress-ai/{repo}/collaborators/{github_username}` with `permission: push`.
4. **Set the origin remote** in the container — `git -C /app/leonardo remote set-url origin https://github.com/llamapress-ai/{repo}.git` (or `add origin` if no remote exists yet).
5. Store the repo name/URL in the instance's env or DB so subsequent calls know provisioning is done.

---

### 4. `.env.example` updates

```
# GitHub App (for automated repo provisioning — different from the OAuth client above)
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=
GITHUB_APP_INSTALLATION_ID=
GITHUB_REPO_NAME=   # Set automatically after provisioning, or pre-set by admin
```

---

### 5. `app/tests/test_github_provisioning.py` (new file)

- Mock the GitHub API calls (httpx mock or `respx`)
- Test: installation token generation (JWT structure, correct claims)
- Test: repo creation skipped if already exists
- Test: collaborator invite fires with correct username
- Test: remote URL set correctly in container
- Test: full `poll_device_auth` flow triggers provisioning on success

---

## User-facing flow after this change

```
Admin creates a new LlamaPress instance (no GitHub steps needed)
         ↓
User opens LlamaBot, clicks "Connect GitHub"
         ↓
Device flow: user enters a code at github.com (existing UX, unchanged)
         ↓
On success:
  • Repo created in llamapress-ai org       (automatic, ~1s)
  • User added as collaborator              (automatic)
  • Origin remote set in their container   (automatic)
         ↓
User gets one GitHub email: "You've been added as a collaborator"
User clicks Accept
         ↓
Done. Push/pull works. Admin did nothing.
```

---

## Open questions

1. **Repo naming convention** — `{instance-slug}` from the DB? `{user-email-prefix}-llamapress`? Should be URL-safe and unique per instance.
2. **Idempotency on re-auth** — If a user re-authenticates (token expired), provisioning should detect the repo exists and skip creation, but re-set the remote URL in case it was lost.
3. **Non-GitHub users** — If a user never authenticates with GitHub, provisioning never fires. Is that acceptable? Or should an admin endpoint (`POST /api/github/provision-repo?instance_id=...`) allow manual trigger without user OAuth?
4. **Collaborator invite vs. no invite** — GitHub sends an invite email that the user must accept. If we want zero user interaction, the alternative is to have the App push/pull on behalf of all users using the installation token only, and never grant individual collaborator access. Worth deciding before implementing.
5. **Private vs. public repos** — Assuming `private: true`. Confirm.
