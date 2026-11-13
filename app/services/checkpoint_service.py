"""Git-based checkpoint service for code rollback functionality."""
import fnmatch
import logging
import os
import subprocess
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path, PurePosixPath

from app.models import CheckpointInfo
from app.services import git_setup
from app.db import engine
from sqlmodel import Session

logger = logging.getLogger(__name__)


# Path to Leonardo repo (mounted in container)
LEONARDO_PATH = Path("/app/leonardo")

# The Leonardo agent runs as uid 1000. git in this container runs as root, so every
# `reset --hard` / `checkout -- .` / `clean -fd` rewrites files as root:root and the
# agent then silently fails on its next edit. Same uid convention as
# `rails_agent/tools.py:chown_for_ubuntu`.
UBUNTU_UID = 1000
UBUNTU_GID = 1000

# Never walk into these — .git is git's own business, node_modules is tens of
# thousands of files (LlamaPress-Simple b855115 hit exactly that stall at boot).
_OWNERSHIP_SKIP_DIRS = {".git", "node_modules"}

# Platform paths, i.e. the files `bin/update` installs rather than the customer's app.
# A checkpoint commits the WHOLE tree, so a restore to a pre-update checkpoint would
# otherwise revert the platform on disk while the containers keep running the new
# images (leo-palevi-dev, 2026-07-27: reverted bin/update, the compose file, the
# langgraph registry and two applied migrations).
#
# This list is deliberately NOT invented here — it mirrors the ALLOWLIST in Leonardo's
# `bin/update` (bin/update:76). Keep the two in sync; if bin/update's allowlist grows,
# grow this one in the same change.
PLATFORM_PATHS = [
    "bin",
    "rails/db/migrate",
    "langgraph/langgraph.json",
    "docker-compose.yml",
    "rails/app/javascript/llamapress",
]


def _configure_git_safe_directory():
    """Configure git to trust the mounted leonardo directory.

    This is needed because the repo owner (host user) differs from
    the container user (root), triggering git's "dubious ownership" check.
    """
    try:
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", str(LEONARDO_PATH)],
            capture_output=True,
            text=True,
            timeout=5
        )
    except Exception:
        pass  # Non-fatal, will fail on git commands if needed


# Configure safe directory on module load
_configure_git_safe_directory()


def _chown_path(path: str) -> bool:
    """lchown one path back to the agent user. Best-effort; never raises."""
    try:
        st = os.lstat(path)
        if st.st_uid == UBUNTU_UID and st.st_gid == UBUNTU_GID:
            return False
        os.lchown(path, UBUNTU_UID, UBUNTU_GID)   # lchown: never follow symlinks
        return True
    except (OSError, PermissionError):
        return False


def _existing_platform_paths(ref: str) -> List[str]:
    """The PLATFORM_PATHS that actually exist at `ref`.

    `git checkout <ref> -- <path>` errors on a pathspec that doesn't exist there, and
    a young repo legitimately lacks some of these.
    """
    present = []
    for path in PLATFORM_PATHS:
        result = subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "cat-file", "-e", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            present.append(path)
    return present


def _reapply_platform_files(pre_rollback_head: str) -> List[str]:
    """Put the platform back the way it was, after a rollback moved it.

    Returns the list of files restored (empty when the checkpoint carried the same
    platform, which is the common case — a same-day restore). Best-effort: the
    customer's code is already back, so a failure here is logged, not raised.
    """
    paths = _existing_platform_paths(pre_rollback_head)
    if not paths:
        return []

    try:
        checkout = subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "checkout", pre_rollback_head, "--", *paths],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if checkout.returncode != 0:
            logger.error(f"Platform re-apply checkout failed: {checkout.stderr}")
            return []

        changed = subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        restored = [f for f in changed.stdout.splitlines() if f.strip()]
        if not restored:
            # Checkpoint had the same platform — nothing to commit, no noise commit.
            return []

        commit = subprocess.run(
            [
                "git", "-C", str(LEONARDO_PATH), "commit",
                "-m",
                "🔧 Re-apply platform files after restore\n\n"
                "A checkpoint commits the whole tree, so restoring one also moves the\n"
                "platform files bin/update installs. The containers keep running the\n"
                "current images, so the platform is put back to match them. Your\n"
                "application code is restored as requested.\n\n"
                f"Files: {', '.join(restored)}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if commit.returncode != 0:
            logger.error(f"Platform re-apply commit failed: {commit.stderr}")
            return []

        logger.info(f"Re-applied {len(restored)} platform file(s) after rollback")
        return restored
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.error(f"Platform re-apply failed: {e}")
        return []


def _restore_ubuntu_ownership() -> int:
    """Give the working tree back to uid 1000 after a root-run git write.

    Walks the tree rather than chowning the paths `git diff --name-only` reports:
    that list is a LOWER BOUND, not the set git actually rewrites. A chown updates
    ctime, which invalidates git's stat cache, so `reset --hard` re-checks-out
    entries whose content never changed. Measured: a two-commit repo where only
    `a.txt` differed still came back with `bin/update` owned by root.

    `.git` and `node_modules` are skipped — `.git` is git's own bookkeeping, and a
    recursive walk of node_modules is the boot stall LlamaPress-Simple hit in
    b855115 ("two-pass setfacl so boot isn't blocked by node_modules walk").

    Best-effort by design: a restore that worked must not be reported as a failure
    because a chown was refused. Returns the number of paths changed.
    """
    changed = 0
    for dirpath, dirnames, filenames in os.walk(LEONARDO_PATH):
        dirnames[:] = [d for d in dirnames if d not in _OWNERSHIP_SKIP_DIRS]
        for name in dirnames + filenames:
            if _chown_path(os.path.join(dirpath, name)):
                changed += 1
    return changed


DISCARD_BACKUP_REF_PREFIX = "refs/llamabot/discards"
# Pre-restore saves (LP-46). After `reset --hard` they sit on no branch, so without a
# ref they would drop out of the History list (`git log --all`) and, later, out of git.
RESTORE_SAVE_REF_PREFIX = "refs/llamabot/saves"
# One ref per discard the user has not yet undone or dismissed, named like its
# backup. It keeps the chat's Undo card up across reloads. Discards from before
# 0.7.15 never got one, so an old snapshot (perhaps already applied by hand)
# never raises a card.
DISCARD_PENDING_REF_PREFIX = "refs/llamabot/discard-pending"

RAILS_CONTROLLERS_DIR = "rails/app/controllers/"
RAILS_MODELS_DIR = "rails/app/models/"
RAILS_VIEWS_DIR = "rails/app/views/"


def _pending_ref(backup_ref: str) -> str:
    return DISCARD_PENDING_REF_PREFIX + backup_ref[len(DISCARD_BACKUP_REF_PREFIX):]


def _snapshot_working_tree(label: str) -> Dict[str, str]:
    """Save every uncommitted file to a snapshot commit, and clear the working tree.

    `git stash push --include-untracked` does both halves at once: it writes the
    modified files AND the untracked ones into a commit, then resets the tree to
    HEAD. Untracked files matter most here — a young customer app is *entirely*
    untracked, which is how a Discard once wiped a whole paying customer's app.

    The stash stack alone is not a safe home for that snapshot: `git stash clear`,
    `git stash pop` or a gc pass can all take it away. So the snapshot commit is
    also pinned under its own `refs/llamabot/discards/*` ref, which keeps it
    reachable forever, independent of the stash stack.

    Raises on any failure — the caller must NOT proceed to destroy anything.
    """
    stash = subprocess.run(
        ["git", "-C", str(LEONARDO_PATH), "stash", "push",
         "--include-untracked", "--message", label],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if stash.returncode != 0:
        raise Exception(f"Git stash failed: {stash.stderr.strip() or stash.stdout.strip()}")

    sha = subprocess.run(
        ["git", "-C", str(LEONARDO_PATH), "rev-parse", "--verify", "refs/stash"],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    if not sha:
        raise Exception("Git stash reported success but wrote no snapshot commit")

    # Microseconds, not seconds: two discards in the same second must still sort
    # deterministically, since the ref name IS the sort key (see list_discard_backups).
    ref = f"{DISCARD_BACKUP_REF_PREFIX}/{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{sha[:8]}"
    pin = subprocess.run(
        ["git", "-C", str(LEONARDO_PATH), "update-ref", ref, sha],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if pin.returncode != 0:
        raise Exception(f"Failed to pin backup ref: {pin.stderr.strip()}")

    logger.info(f"Snapshotted working tree to {ref} ({sha[:8]}) before discard")
    return {"ref": ref, "sha": sha}


# ============== Saving (LP-46) ==============
#
# Leo commits on its own at the end of every turn that changed files. Without it,
# a box where the user never clicked Save showed a growing "669 unsaved changes"
# that nothing on screen could fix except Discard — which deletes working code.

# One lock around "check status, stage, commit" — and around restore and discard —
# shared by the end-of-turn auto-save and the manual Save / Restore routes, so two
# saves at the same moment make one commit and neither errors. Re-entrant because a
# restore saves first.
REPO_LOCK = threading.RLock()

# Auto-saves commit as Leo, set per commit, so they work on a box with no git
# identity and never touch the box's git config.
LEO_AUTHOR_NAME = "Leo"
LEO_AUTHOR_EMAIL = "leo@llamapress.local"

# The first auto-save on a box with weeks of uncommitted work commits all of it at once.
BACKLOG_TITLE = "Saved all earlier work"

# Larger files stay out of saves (logged), so one stray video can't bloat .git.
MAX_SAVE_FILE_BYTES = 50 * 1024 * 1024

# Never committed, whatever .gitignore says: secrets, uploads, logs, scratch.
_NEVER_SAVED_NAMES = (".env", ".env.*", "*.pem", "*.key")
_NEVER_SAVED_NAME_EXCEPTIONS = (".env.example",)
# Rails' storage/ (uploads), log/ and tmp/, at the repo root or in the rails/ app.
_NEVER_SAVED_DIRS = ("storage", "log", "tmp")

INDEX_LOCK_WAIT_SECONDS = 2.0

# What the History panel says when a save failed. Git's stderr goes to the log only.
SAVE_FAILED_SENTENCE = "Leo couldn't save your latest changes. It will try again after the next turn."

_last_save_error: Optional[str] = None
_has_leo_save: Optional[bool] = None    # cached once True: has Leo ever saved on this box?
_backlog_pending = False                # this turn started with work nobody had saved


def is_never_saved(path: str) -> bool:
    """True for a path no save may commit (a secret, an upload, a log, scratch)."""
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    name = parts[-1]
    if name not in _NEVER_SAVED_NAME_EXCEPTIONS and any(fnmatch.fnmatch(name, p) for p in _NEVER_SAVED_NAMES):
        return True
    if len(parts) > 1 and parts[0] in _NEVER_SAVED_DIRS:
        return True
    return len(parts) > 2 and parts[0] == "rails" and parts[1] in _NEVER_SAVED_DIRS


def _git_run(*args: str, timeout: int = 60, input: Optional[str] = None) -> subprocess.CompletedProcess:
    """git in the Leonardo repo; if another git holds index.lock, wait and retry once."""
    def once():
        return subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), *args],
            capture_output=True, text=True, timeout=timeout, input=input,
        )

    result = once()
    if result.returncode != 0 and "index.lock" in (result.stderr or ""):
        time.sleep(INDEX_LOCK_WAIT_SECONDS)
        result = once()
    return result


def _changed_paths() -> List[str]:
    """Every path git sees as changed, new or deleted (both sides of a rename)."""
    result = _git_run("status", "--porcelain=v1", "-z", "--untracked-files=all", timeout=30)
    if result.returncode != 0:
        raise Exception(f"Git status failed: {result.stderr.strip()}")
    tokens = result.stdout.split("\0")
    paths, i = [], 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and i < len(tokens):  # -z puts a rename's old path next
            paths.append(tokens[i])
            i += 1
    return paths


def _too_big(path: str) -> bool:
    try:
        return (LEONARDO_PATH / path).lstat().st_size > MAX_SAVE_FILE_BYTES
    except OSError:  # deleted: staging the deletion is fine
        return False


def _split_savable(paths: List[str]):
    """(paths a save may commit, [(path, why) left out])."""
    keep, dropped = [], []
    for path in paths:
        if is_never_saved(path):
            dropped.append((path, "secret or upload"))
        elif _too_big(path):
            dropped.append((path, f"over {MAX_SAVE_FILE_BYTES // (1024 * 1024)} MB"))
        else:
            keep.append(path)
    return keep, dropped


def _stage_savable() -> None:
    """Stage what a save may commit, then make sure nothing else is staged."""
    keep, dropped = _split_savable(_changed_paths())
    for path, why in dropped:
        logger.warning(f"Save left out {path} ({why})")
    if keep:
        added = _git_run("--literal-pathspecs", "add", "-A", "--pathspec-from-file=-",
                         "--pathspec-file-nul", input="\0".join(keep))
        if added.returncode != 0:
            raise Exception(f"Git add failed: {added.stderr.strip()}")

    # Belt and braces: anything already staged by someone else is checked too.
    staged = _git_run("diff", "--cached", "--name-only", "-z", timeout=30).stdout.split("\0")
    _, refused = _split_savable([p for p in staged if p])
    if not refused:
        return
    paths = [p for p, _ in refused]
    for path, why in refused:
        if path not in {d for d, _ in dropped}:
            logger.warning(f"Save unstaged {path} ({why})")
    has_head = _git_run("rev-parse", "--verify", "-q", "HEAD", timeout=5).returncode == 0
    unstage = (["reset", "-q", "HEAD", "--"] if has_head
               else ["rm", "--cached", "-r", "-q", "--ignore-unmatch", "--"])
    result = _git_run("--literal-pathspecs", *unstage, *paths)
    if result.returncode != 0:
        raise Exception(f"Could not unstage a refused path: {result.stderr.strip()}")


def _anything_staged() -> bool:
    return _git_run("diff", "--cached", "--quiet", timeout=30).returncode == 1


def _leo_has_saved() -> bool:
    global _has_leo_save
    if not _has_leo_save:
        found = _git_run("log", "-1", "--format=%H", f"--author={re.escape(LEO_AUTHOR_EMAIL)}", timeout=30)
        _has_leo_save = found.returncode == 0 and bool(found.stdout.strip())
    return _has_leo_save


def turn_title(request_text: Optional[str], now: Optional[datetime] = None) -> str:
    """`Leo: <start of the user's request> · 2026-11-10 14:32 UTC`, on one clean line."""
    now = now or datetime.now(timezone.utc)
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in (request_text or ""))
    text = " ".join(text.split())[:60].rstrip() or "Saved Leo's changes"
    return f"Leo: {text} · {now:%Y-%m-%d %H:%M} UTC"


def note_turn_start() -> None:
    """Before a turn: does it start on top of work nobody ever saved? (Until Leo's first save.)"""
    global _backlog_pending
    try:
        _backlog_pending = (not _leo_has_saved()) and bool(_split_savable(_changed_paths())[0])
    except Exception as e:  # noqa: BLE001 - never block a turn on this
        logger.debug(f"Backlog check skipped: {e}")


def autosave_after_turn(request_text: Optional[str], thread_id: str = "") -> Optional[Dict[str, Any]]:
    """The end-of-turn save: one commit if anything changed. Never raises.

    A failure is logged and shown in the History panel; the tree stays dirty, so
    the next turn tries again.
    """
    global _last_save_error, _has_leo_save, _backlog_pending
    try:
        with REPO_LOCK:
            title = BACKLOG_TITLE if _backlog_pending and not _leo_has_saved() else turn_title(request_text)
            result = CheckpointService.create_checkpoint(
                thread_id=thread_id or "auto", description=title, leo=True, skip_if_clean=True,
            )
        if not result.get("skipped"):
            _has_leo_save = True
            _backlog_pending = False
        return result
    except Exception as e:  # noqa: BLE001 - a save must never break a turn
        logger.error(f"Auto-save failed on box {os.getenv('INSTANCE_NAME', '?')} "
                     f"(thread {thread_id}): {e}")
        _last_save_error = SAVE_FAILED_SENTENCE
        return None


AUTOSAVE_SETTING_KEY = "leo_autosave_enabled"


def autosave_enabled() -> bool:
    """Should Leo commit at the end of a turn? (LP-49) Read live, so a toggle needs no restart.

    LEO_AUTOSAVE_DISABLED=1 (the test suite's switch) wins. Otherwise only the exact
    value "false" turns it off: no row, no auth DB, or an unreadable value means ON.
    Nothing writes a row by default, so the fleet default stays in this code.
    """
    if os.getenv("LEO_AUTOSAVE_DISABLED") == "1":
        return False
    try:
        from sqlmodel import Session as DbSession  # this module's `Session` is the checkpoint DB's
        from app import db
        from app.models import SiteSetting
        if db.engine is None:
            return True
        with DbSession(db.engine) as session:
            row = session.get(SiteSetting, AUTOSAVE_SETTING_KEY)
        return row is None or str(row.value).strip().lower() != "false"
    except Exception as e:  # noqa: BLE001 - never block a save on reading a switch
        logger.warning(f"Could not read {AUTOSAVE_SETTING_KEY}, auto-save stays on: {e}")
        return True


def save_status(turn_in_progress: bool = False) -> Dict[str, Any]:
    """What the History panel shows. `clean` is live git status, never "a save was tried"."""
    changed, _ = _split_savable(_changed_paths())
    last = _git_run("log", "-1", "--format=%ct", timeout=10).stdout.strip()
    last_saved_at = (
        datetime.fromtimestamp(int(last), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if last else None
    )
    return {
        "clean": not changed,
        "last_saved_at": last_saved_at,
        "turn_in_progress": bool(turn_in_progress),
        "changed_files": len(changed),
        "last_save_error": _last_save_error,
        "autosave_enabled": autosave_enabled(),
    }


class CheckpointService:
    """Service for managing git-based checkpoints for code rollback."""

    @staticmethod
    def create_checkpoint(
        thread_id: str,
        description: str,
        *,
        leo: bool = False,
        skip_if_clean: bool = False,
    ) -> Dict[str, Any]:
        """Create a git checkpoint (commit) of the working tree.

        Args:
            thread_id: The conversation thread ID
            description: Human-readable description (for ``leo`` saves, the subject line)
            leo: Commit as Leo (auto-saves, pre-restore saves) instead of the box's identity
            skip_if_clean: Make no commit when nothing is staged; returns ``skipped: True``

        Secrets, uploads and oversized files are never staged (LP-46).

        Returns:
            Dictionary with checkpoint info including commit SHA

        Raises:
            Exception: If git operations fail
        """
        global _last_save_error
        with REPO_LOCK:
            result = CheckpointService._create_checkpoint_locked(thread_id, description, leo, skip_if_clean)
        _last_save_error = None
        return result

    @staticmethod
    def _create_checkpoint_locked(thread_id, description, leo, skip_if_clean) -> Dict[str, Any]:
        try:
            _stage_savable()

            if skip_if_clean and not _anything_staged():
                head = _git_run("rev-parse", "HEAD", timeout=5).stdout.strip()
                return {"checkpoint_id": head, "thread_id": thread_id, "description": description,
                        "skipped": True, "changed_files": [], "changed_files_count": 0}

            timestamp = datetime.now(timezone.utc).isoformat()
            subject = description if leo else f"🔖 Checkpoint: {description}"
            commit_message = f"""{subject}

Thread: {thread_id}
Agent: Leonardo
Timestamp: {timestamp}
"""
            identity = (["-c", f"user.name={LEO_AUTHOR_NAME}", "-c", f"user.email={LEO_AUTHOR_EMAIL}"]
                        if leo else [])

            def commit():
                return _git_run(*identity, "commit", "-m", commit_message, "--allow-empty")

            commit_result = commit()

            # A box where git doesn't know who is committing (LP-20): set the
            # missing identity the way /git-config would, and retry once.
            if commit_result.returncode != 0 and git_setup.is_missing_identity(commit_result.stderr):
                logger.info("Checkpoint commit needs a git identity; setting it up and retrying")
                git_setup.ensure_git_identity()
                commit_result = commit()

            if commit_result.returncode != 0:
                # Check if it's just "nothing to commit"
                if "nothing to commit" in commit_result.stdout.lower():
                    # Get current HEAD SHA
                    sha_result = subprocess.run(
                        ["git", "-C", str(LEONARDO_PATH), "rev-parse", "HEAD"],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    checkpoint_id = sha_result.stdout.strip()
                else:
                    raise Exception(f"Git commit failed: {commit_result.stderr}")
            else:
                # Extract commit SHA from output
                sha_result = subprocess.run(
                    ["git", "-C", str(LEONARDO_PATH), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                checkpoint_id = sha_result.stdout.strip()

            # Get changed files count
            changed_files = CheckpointService._get_changed_files_internal(checkpoint_id)

            # Save to database
            with Session(engine) as session:
                checkpoint = CheckpointInfo(
                    checkpoint_id=checkpoint_id,
                    thread_id=thread_id,
                    description=description,
                    changed_files_count=len(changed_files)
                )
                session.add(checkpoint)
                session.commit()
                session.refresh(checkpoint)

            return {
                "checkpoint_id": checkpoint_id,
                "thread_id": thread_id,
                "description": description,
                "created_at": checkpoint.created_at.isoformat(),
                "changed_files": changed_files,
                "changed_files_count": len(changed_files)
            }

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to create checkpoint: {str(e)}")

    @staticmethod
    def list_checkpoints(thread_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all checkpoints (optionally filtered by thread).

        Args:
            thread_id: Optional conversation thread ID to filter by

        Returns:
            List of checkpoint dictionaries
        """
        with Session(engine) as session:
            query = session.query(CheckpointInfo)

            # Only filter by thread_id if provided
            if thread_id:
                query = query.filter(CheckpointInfo.thread_id == thread_id)

            checkpoints = query.order_by(CheckpointInfo.created_at.desc()).all()

            return [
                {
                    "checkpoint_id": cp.checkpoint_id,
                    "thread_id": cp.thread_id,
                    "description": cp.description,
                    "created_at": cp.created_at.isoformat(),
                    "is_accepted": cp.is_accepted,
                    "changed_files_count": cp.changed_files_count
                }
                for cp in checkpoints
            ]

    @staticmethod
    def get_checkpoint_diff(checkpoint_id: str) -> Dict[str, Any]:
        """Get the diff for a specific checkpoint.

        Args:
            checkpoint_id: Git commit SHA

        Returns:
            Dictionary with diff statistics and content
        """
        try:
            # Get diff statistics
            stat_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "show", checkpoint_id, "--stat"],
                capture_output=True,
                text=True,
                timeout=10
            )

            # Get full diff
            diff_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "show", checkpoint_id],
                capture_output=True,
                text=True,
                timeout=10
            )

            # Get changed files list
            files_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "show", checkpoint_id, "--name-only", "--format="],
                capture_output=True,
                text=True,
                timeout=10
            )

            changed_files = [f.strip() for f in files_result.stdout.split("\n") if f.strip()]

            return {
                "checkpoint_id": checkpoint_id,
                "stat": stat_result.stdout,
                "diff": diff_result.stdout,
                "changed_files": changed_files
            }

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to get diff: {str(e)}")

    @staticmethod
    def rollback_to_checkpoint(checkpoint_id: str, report: bool = False):
        """Rollback to a specific checkpoint (hard reset).

        A checkpoint commits the WHOLE Leonardo tree — the customer's application AND
        the platform files `bin/update` installs. Restoring a checkpoint taken before a
        platform update would therefore silently revert the platform on disk while the
        containers keep running the new images. So after the reset we put the platform
        paths back from the pre-rollback HEAD, as one labelled commit: the customer gets
        his application code back and keeps a working platform.

        Args:
            checkpoint_id: Git commit SHA to rollback to
            report: When True, return a dict describing what happened instead of a bool.

        Returns:
            True if successful (or a dict when ``report=True``)

        Raises:
            Exception: If rollback fails
        """
        try:
            # Validate checkpoint exists
            check_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "cat-file", "-e", f"{checkpoint_id}^{{commit}}"],
                capture_output=True,
                text=True,
                timeout=5
            )

            if check_result.returncode != 0:
                raise Exception(f"Checkpoint {checkpoint_id} does not exist")

            # Remember where the platform currently is, BEFORE the reset moves it.
            pre_rollback_head = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5
            ).stdout.strip()

            # Hard reset to checkpoint
            reset_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "reset", "--hard", checkpoint_id],
                capture_output=True,
                text=True,
                timeout=10
            )

            if reset_result.returncode != 0:
                raise Exception(f"Git reset failed: {reset_result.stderr}")

            # Put the platform back where the running containers expect it.
            platform_files = (
                _reapply_platform_files(pre_rollback_head) if pre_rollback_head else []
            )

            # git ran as root, so every rewritten file is now root-owned and the
            # uid-1000 agent can no longer edit it (leo-palevi-dev, 2026-07-27).
            # Runs LAST so it also covers the platform re-apply.
            fixed = _restore_ubuntu_ownership()
            if fixed:
                logger.info(f"Restored agent ownership on {fixed} path(s) after rollback")

            # Clean untracked files (optional, commented out for safety)
            # clean_result = subprocess.run(
            #     ["git", "-C", str(LEONARDO_PATH), "clean", "-fd"],
            #     capture_output=True,
            #     text=True,
            #     timeout=10
            # )

            if report:
                return {
                    "success": True,
                    "checkpoint_id": checkpoint_id,
                    "platform_files_reapplied": platform_files,
                }
            return True

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to rollback: {str(e)}")

    @staticmethod
    def restore_checkpoint(checkpoint_id: str) -> Dict[str, Any]:
        """Go back to a checkpoint, saving the current state first so it can be undone.

        The save is skipped on a clean tree (HEAD already holds it). If the save
        fails, nothing is restored. ``saved_before_restore`` is the new commit, or None.
        """
        with REPO_LOCK:
            if _git_run("cat-file", "-e", f"{checkpoint_id}^{{commit}}", timeout=5).returncode != 0:
                raise Exception(f"Checkpoint {checkpoint_id} does not exist")
            name = _git_run("log", "-1", "--format=%s", checkpoint_id, timeout=5).stdout.strip()[:60]
            pre = CheckpointService.create_checkpoint(
                thread_id="restore", description=f"Before going back to {name or checkpoint_id[:8]}",
                leo=True, skip_if_clean=True,
            )
            if not pre.get("skipped"):
                sha = pre["checkpoint_id"]
                ref = f"{RESTORE_SAVE_REF_PREFIX}/{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{sha[:8]}"
                pin = _git_run("update-ref", ref, sha, timeout=5)
                if pin.returncode != 0:
                    raise Exception(f"Could not pin the pre-restore save: {pin.stderr.strip()}")
            result = CheckpointService.rollback_to_checkpoint(checkpoint_id, report=True)
        result["saved_before_restore"] = None if pre.get("skipped") else pre["checkpoint_id"]
        return result

    @staticmethod
    def get_changed_files(since_checkpoint: Optional[str] = None) -> List[str]:
        """Get list of files changed since a checkpoint.

        Args:
            since_checkpoint: Git commit SHA to compare against (default: HEAD)

        Returns:
            List of changed file paths
        """
        try:
            if since_checkpoint:
                cmd = ["git", "-C", str(LEONARDO_PATH), "diff", "--name-only", since_checkpoint, "HEAD"]
            else:
                # Get uncommitted changes
                cmd = ["git", "-C", str(LEONARDO_PATH), "diff", "--name-only", "HEAD"]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return []

            changed_files = [f.strip() for f in result.stdout.split("\n") if f.strip()]
            return changed_files

        except Exception:
            return []

    @staticmethod
    def _get_changed_files_internal(checkpoint_id: str) -> List[str]:
        """Internal helper to get changed files for a checkpoint."""
        try:
            result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "show", checkpoint_id, "--name-only", "--format="],
                capture_output=True,
                text=True,
                timeout=10
            )
            return [f.strip() for f in result.stdout.split("\n") if f.strip()]
        except Exception:
            return []

    @staticmethod
    def mark_checkpoint_accepted(checkpoint_id: str) -> bool:
        """Mark a checkpoint as accepted by the user.

        Args:
            checkpoint_id: Git commit SHA

        Returns:
            True if successful
        """
        with Session(engine) as session:
            checkpoint = session.query(CheckpointInfo).filter(
                CheckpointInfo.checkpoint_id == checkpoint_id
            ).first()

            if checkpoint:
                checkpoint.is_accepted = True
                session.commit()
                return True
            return False

    @staticmethod
    def mark_checkpoint_rejected(checkpoint_id: str) -> bool:
        """Mark a checkpoint as rejected by the user.

        Args:
            checkpoint_id: Git commit SHA

        Returns:
            True if successful
        """
        with Session(engine) as session:
            checkpoint = session.query(CheckpointInfo).filter(
                CheckpointInfo.checkpoint_id == checkpoint_id
            ).first()

            if checkpoint:
                checkpoint.is_accepted = False
                session.commit()
                return True
            return False

    @staticmethod
    def get_current_head() -> str:
        """Get the current git HEAD commit SHA.

        Returns:
            Current HEAD commit SHA
        """
        try:
            result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                return result.stdout.strip()
            else:
                raise Exception(f"Failed to get HEAD: {result.stderr}")

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to get current HEAD: {str(e)}")

    @staticmethod
    def get_uncommitted_changes() -> dict:
        """Check for uncommitted changes in the working directory.

        Returns:
            Dict with:
                - has_changes: bool
                - changed_files: list of changed file paths
                - untracked_files: list of new untracked files
                - total_count, controller_count, model_count, view_count
                - untracked_controllers / untracked_models: True if any of them
                  was never committed, i.e. discarding deletes it outright
        """
        try:
            # Get modified/deleted files (staged and unstaged)
            diff_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "diff", "--name-only", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5
            )
            changed_files = [f.strip() for f in diff_result.stdout.split("\n") if f.strip()]

            # Get untracked files
            untracked_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "ls-files", "--others", "--exclude-standard"],
                capture_output=True,
                text=True,
                timeout=5
            )
            untracked_files = [f.strip() for f in untracked_result.stdout.split("\n") if f.strip()]

            has_changes = len(changed_files) > 0 or len(untracked_files) > 0
            all_files = changed_files + untracked_files

            def under(files, prefix):
                return [f for f in files if f.startswith(prefix)]

            return {
                "has_changes": has_changes,
                "changed_files": changed_files,
                "untracked_files": untracked_files,
                "total_count": len(all_files),
                # How much of the app a discard takes, for the UI's warning.
                "controller_count": len(under(all_files, RAILS_CONTROLLERS_DIR)),
                "model_count": len(under(all_files, RAILS_MODELS_DIR)),
                "view_count": len(under(all_files, RAILS_VIEWS_DIR)),
                "untracked_controllers": bool(under(untracked_files, RAILS_CONTROLLERS_DIR)),
                "untracked_models": bool(under(untracked_files, RAILS_MODELS_DIR)),
            }

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to check for uncommitted changes: {str(e)}")

    @staticmethod
    def discard_uncommitted_changes() -> dict:
        """Discard all uncommitted changes (reset to HEAD), keeping a way back.

        A snapshot of everything about to be thrown away is taken FIRST, and the
        discard is abandoned if that snapshot cannot be written. Without it this was
        a one-click, no-confirmation, unrecoverable delete of a customer's whole app.

        This performs:
        - git stash push --include-untracked (snapshot + reset the tree to HEAD)
        - git checkout -- . / git clean -fd (belt-and-braces, normally no-ops)

        Returns:
            Dict with discarded file counts plus `backup_ref` / `backup_sha`, which
            `restore_discarded_changes` takes to put it all back.
        """
        try:
            # First get what we're about to discard for reporting
            changes = CheckpointService.get_uncommitted_changes()

            if not changes["has_changes"]:
                return {
                    "success": True,
                    "message": "No changes to discard",
                    "discarded_count": 0,
                    "backup_ref": None,
                    "backup_sha": None,
                }

            # Snapshot BEFORE destroying anything. If this raises, nothing is lost.
            try:
                backup = _snapshot_working_tree(
                    f"llamabot-discard {datetime.now(timezone.utc).isoformat()} "
                    f"({changes['total_count']} file(s))"
                )
            except Exception as e:
                raise Exception(
                    f"Refusing to discard: could not back up your changes first ({e})"
                )

            # Keep the chat's Undo card up until the user undoes or dismisses it.
            # Not fatal: the snapshot is what matters, the card is the way back to it.
            mark = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "update-ref",
                 _pending_ref(backup["ref"]), backup["sha"]],
                capture_output=True, text=True, timeout=5,
            )
            if mark.returncode != 0:
                logger.warning(f"Could not mark discard {backup['ref']} pending: {mark.stderr.strip()}")

            # Discard modified files (checkout to HEAD)
            checkout_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "checkout", "--", "."],
                capture_output=True,
                text=True,
                timeout=10
            )

            if checkout_result.returncode != 0:
                raise Exception(f"Git checkout failed: {checkout_result.stderr}")

            # Remove untracked files and directories
            clean_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "clean", "-fd"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if clean_result.returncode != 0:
                raise Exception(f"Git clean failed: {clean_result.stderr}")

            # Both git calls above ran as root, so anything `checkout` restored is
            # now root-owned; hand the tree back to the agent user.
            fixed = _restore_ubuntu_ownership()
            if fixed:
                logger.info(f"Restored agent ownership on {fixed} path(s) after discard")

            return {
                "success": True,
                "message": f"Discarded {changes['total_count']} file(s)",
                "discarded_modified": len(changes["changed_files"]),
                "discarded_untracked": len(changes["untracked_files"]),
                "discarded_count": changes["total_count"],
                "backup_ref": backup["ref"],
                "backup_sha": backup["sha"],
                "undo_command": f"git stash apply {backup['sha']}",
            }

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to discard changes: {str(e)}")

    @staticmethod
    def list_discard_backups(limit: int = 20) -> List[Dict[str, Any]]:
        """The snapshots taken by past discards, newest first.

        Returns:
            List of dicts with ref, sha, created_at, file_count, and pending (the
            user has neither undone nor dismissed it yet).
        """
        result = subprocess.run(
            [
                "git", "-C", str(LEONARDO_PATH), "for-each-ref",
                # By ref name, which embeds a microsecond timestamp: `creatordate`
                # ties whenever two discards land in the same second.
                "--sort=-refname",
                f"--count={limit}",
                "--format=%(refname)%09%(objectname)%09%(creatordate:iso-strict)",
                DISCARD_BACKUP_REF_PREFIX,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.error(f"Failed to list discard backups: {result.stderr}")
            return []

        pending = subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "for-each-ref",
             "--format=%(refname)", DISCARD_PENDING_REF_PREFIX],
            capture_output=True, text=True, timeout=10,
        )
        pending_refs = set(pending.stdout.split()) if pending.returncode == 0 else set()

        backups = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            ref, sha, created_at = line.split("\t")
            backups.append({
                "ref": ref,
                "sha": sha,
                "created_at": created_at,
                "file_count": len(CheckpointService._backup_files(sha)),
                "pending": _pending_ref(ref) in pending_refs,
            })
        return backups

    @staticmethod
    def dismiss_discard_backup(backup_ref: str) -> dict:
        """Take down the chat's Undo card for one discard. The snapshot is kept."""
        if not backup_ref.startswith(DISCARD_BACKUP_REF_PREFIX + "/"):
            return {"success": False, "message": f"Not a discard backup: {backup_ref}"}
        subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "update-ref", "-d", _pending_ref(backup_ref)],
            capture_output=True, text=True, timeout=5,
        )
        return {"success": True, "backup_ref": backup_ref}

    @staticmethod
    def _backup_files(sha: str) -> List[str]:
        """The files held in a discard snapshot — tracked edits and untracked alike."""
        files = set()
        # Tracked changes: the snapshot commit against its first parent (HEAD then).
        tracked = subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "diff", "--name-only", f"{sha}^1", sha],
            capture_output=True, text=True, timeout=15,
        )
        files.update(f for f in tracked.stdout.splitlines() if f.strip())
        # Untracked files live in the stash's third parent, when there is one.
        untracked = subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "ls-tree", "-r", "--name-only", f"{sha}^3"],
            capture_output=True, text=True, timeout=15,
        )
        if untracked.returncode == 0:
            files.update(f for f in untracked.stdout.splitlines() if f.strip())
        return sorted(files)

    @staticmethod
    def restore_discarded_changes(backup_ref: Optional[str] = None) -> dict:
        """Undo a discard: put the snapshotted working tree back.

        Args:
            backup_ref: Which snapshot to restore. Defaults to the most recent one.

        Returns:
            Dict with success flag, a human message and the restored file list. A
            snapshot that cannot be applied cleanly (because the tree has moved on
            since) is reported as a failure, never forced.
        """
        try:
            if backup_ref is None:
                backups = CheckpointService.list_discard_backups(limit=1)
                if not backups:
                    return {
                        "success": False,
                        "message": "No discarded changes to restore",
                        "restored_files": [],
                    }
                backup_ref = backups[0]["ref"]

            sha = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "rev-parse", "--verify", f"{backup_ref}^{{commit}}"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if not sha:
                return {
                    "success": False,
                    "message": f"No such backup: {backup_ref}",
                    "restored_files": [],
                }

            files = CheckpointService._backup_files(sha)

            apply_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "stash", "apply", sha],
                capture_output=True, text=True, timeout=60,
            )
            if apply_result.returncode != 0:
                return {
                    "success": False,
                    "message": (
                        "Could not restore automatically — the files have changed "
                        f"since they were discarded. Your snapshot is safe at {sha[:8]}. "
                        f"Details: {apply_result.stderr.strip()}"
                    ),
                    "backup_ref": backup_ref,
                    "backup_sha": sha,
                    "restored_files": [],
                }

            # git ran as root here too, so the agent must get the tree back.
            fixed = _restore_ubuntu_ownership()
            if fixed:
                logger.info(f"Restored agent ownership on {fixed} path(s) after undo")

            if backup_ref.startswith(DISCARD_BACKUP_REF_PREFIX + "/"):
                CheckpointService.dismiss_discard_backup(backup_ref)

            logger.info(f"Restored {len(files)} discarded file(s) from {backup_ref}")
            return {
                "success": True,
                "message": f"Restored {len(files)} file(s)",
                "backup_ref": backup_ref,
                "backup_sha": sha,
                "restored_files": files,
            }

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to restore discarded changes: {str(e)}")

    @staticmethod
    def get_git_graph(limit: int = 50) -> Dict[str, Any]:
        """Get commit history with branch topology for visualization.

        Returns commit data with lane assignments for rendering a git graph
        similar to SourceTree/GitKraken.

        Args:
            limit: Maximum number of commits to return

        Returns:
            Dictionary with:
                - commits: List of commit objects with topology info
                - branches: List of branch metadata with colors
                - max_branch_index: Maximum lane index used
        """
        try:
            # Get commit history with parent info for topology
            # Format: SHA|short_sha|parent_shas|subject|author|timestamp|refs
            result = subprocess.run(
                [
                    "git", "-C", str(LEONARDO_PATH), "log",
                    f"--max-count={limit}",
                    "--format=%H|%h|%P|%s|%an|%aI|%D",
                    "--topo-order",
                    "--all"
                ],
                capture_output=True,
                text=True,
                timeout=15
            )

            if result.returncode != 0:
                raise Exception(f"Git log failed: {result.stderr}")

            commits = []
            commit_to_lane = {}  # SHA -> lane index
            lane_heads = []  # Current commit SHA at each lane (None if lane is free)
            branch_colors = ["#8b5cf6", "#22c55e", "#eab308", "#3b82f6", "#ef4444", "#ec4899", "#14b8a6"]

            lines = [line for line in result.stdout.strip().split('\n') if line]

            for line in lines:
                parts = line.split('|')
                if len(parts) < 6:
                    continue

                sha = parts[0]
                short_sha = parts[1]
                parent_shas = parts[2].split() if parts[2] else []
                subject = parts[3]
                author = parts[4]
                timestamp = parts[5]
                refs = parts[6] if len(parts) > 6 else ""

                # Parse refs (e.g., "HEAD -> main, origin/main")
                ref_list = []
                if refs:
                    for ref in refs.split(', '):
                        ref = ref.strip()
                        if ref:
                            # Clean up refs like "HEAD -> main"
                            if ' -> ' in ref:
                                ref_list.extend(ref.split(' -> '))
                            else:
                                ref_list.append(ref)
                # Our own bookkeeping refs (discard backups, pre-restore saves) are
                # not branches the user made; keep them out of the badges.
                ref_list = [r for r in ref_list if not r.startswith("refs/llamabot/")]

                # Determine lane for this commit
                # First check if any child commit assigned us a lane
                if sha in commit_to_lane:
                    lane_index = commit_to_lane[sha]
                else:
                    # Find first free lane or create new one
                    lane_index = None
                    for i, head in enumerate(lane_heads):
                        if head is None or head == sha:
                            lane_index = i
                            break
                    if lane_index is None:
                        lane_index = len(lane_heads)
                        lane_heads.append(None)

                # Update lane head
                if lane_index < len(lane_heads):
                    lane_heads[lane_index] = sha
                else:
                    lane_heads.append(sha)

                # Assign parent commits to lanes
                merge_lines = []
                for i, parent_sha in enumerate(parent_shas):
                    if i == 0:
                        # First parent stays in same lane
                        commit_to_lane[parent_sha] = lane_index
                        if lane_index < len(lane_heads):
                            lane_heads[lane_index] = parent_sha
                    else:
                        # Additional parents (merge) - find or create lane
                        if parent_sha in commit_to_lane:
                            parent_lane = commit_to_lane[parent_sha]
                        else:
                            # Find free lane for this parent
                            parent_lane = None
                            for j, head in enumerate(lane_heads):
                                if head is None and j != lane_index:
                                    parent_lane = j
                                    break
                            if parent_lane is None:
                                parent_lane = len(lane_heads)
                                lane_heads.append(None)
                            commit_to_lane[parent_sha] = parent_lane
                            lane_heads[parent_lane] = parent_sha

                        merge_lines.append({
                            "from_lane": lane_index,
                            "to_lane": parent_lane,
                            "parent_sha": parent_sha
                        })

                # Get changed files count for this commit
                changed_files_count = len(CheckpointService._get_changed_files_internal(sha))

                commits.append({
                    "sha": sha,
                    "short_sha": short_sha,
                    "subject": subject,
                    "author": author,
                    "timestamp": timestamp,
                    "parent_shas": parent_shas,
                    "branch_index": lane_index,
                    "is_merge": len(parent_shas) > 1,
                    "refs": ref_list,
                    "changed_files_count": changed_files_count,
                    "merge_lines": merge_lines
                })

            # Calculate max branch index
            max_branch_index = max((c["branch_index"] for c in commits), default=0)

            # Build branch info
            branches = []
            seen_refs = set()
            for commit in commits:
                for ref in commit["refs"]:
                    if ref not in seen_refs and not ref.startswith("origin/"):
                        seen_refs.add(ref)
                        branches.append({
                            "name": ref,
                            "color": branch_colors[len(branches) % len(branch_colors)],
                            "index": commit["branch_index"]
                        })

            return {
                "commits": commits,
                "branches": branches,
                "max_branch_index": max_branch_index
            }

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to get git graph: {str(e)}")


# Singleton instance
checkpoint_service = CheckpointService()
