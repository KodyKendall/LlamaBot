"""API routes for git-based checkpoint/rollback functionality."""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services.checkpoint_service import checkpoint_service

logger = logging.getLogger(__name__)

router = APIRouter()

# Path to the Leonardo repo mounted into the llamabot container. The Rails app
# code (rails/app, routes.rb, db/) is bind-mounted from this same checkout into
# the llamapress container, so pulling here + restarting llamapress is all it
# takes to update the running app. (See checkpoint_service for git safe.directory
# setup, which runs on import of this module.)
LEONARDO_PATH = Path("/app/leonardo")

# Friendly, non-technical message shown when a fast-forward pull is refused
# because the box has local edits that conflict with the incoming update.
FF_BLOCKED_MESSAGE = (
    "Can't auto-update — you have file changes on this version that conflict "
    "with the updated version. Commit or discard them before pulling this update."
)


# ============== Pydantic Models ==============

class CreateCheckpointRequest(BaseModel):
    thread_id: Optional[str] = "manual"
    description: str


class RollbackRequest(BaseModel):
    checkpoint_id: str


class UndoDiscardRequest(BaseModel):
    # Which discard snapshot to restore; omitted means the most recent one.
    backup_ref: Optional[str] = None


# ============== Checkpoint Endpoints ==============

@router.post("/api/checkpoints")
def create_checkpoint(
    request: CreateCheckpointRequest
):
    """Create a new git checkpoint before AI agent makes changes.

    Args:
        request: CreateCheckpointRequest with thread_id and description
        current_user: Authenticated user

    Returns:
        Checkpoint information including commit SHA
    """
    try:
        checkpoint = checkpoint_service.create_checkpoint(
            thread_id=request.thread_id,
            description=request.description
        )
        return JSONResponse(content=checkpoint, status_code=201)

    except Exception as e:
        logger.error(f"Failed to create checkpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/checkpoints")
def list_checkpoints(
    thread_id: Optional[str] = None
):
    """List all checkpoints (optionally filtered by thread).

    Args:
        thread_id: Optional conversation thread ID to filter by
        current_user: Authenticated user

    Returns:
        List of checkpoints with metadata
    """
    try:
        checkpoints = checkpoint_service.list_checkpoints(thread_id=thread_id)
        return JSONResponse(content={"checkpoints": checkpoints})

    except Exception as e:
        logger.error(f"Failed to list checkpoints: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/checkpoints/{checkpoint_id}/diff")
def get_checkpoint_diff(
    checkpoint_id: str
):
    """Get the git diff for a specific checkpoint.

    Args:
        checkpoint_id: Git commit SHA
        current_user: Authenticated user

    Returns:
        Diff statistics and content
    """
    try:
        diff_data = checkpoint_service.get_checkpoint_diff(checkpoint_id)
        return JSONResponse(content=diff_data)

    except Exception as e:
        logger.error(f"Failed to get checkpoint diff: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/checkpoints/{checkpoint_id}/rollback")
def rollback_checkpoint(
    checkpoint_id: str
):
    """Rollback to a specific checkpoint (hard reset).

    Args:
        checkpoint_id: Git commit SHA to rollback to
        current_user: Authenticated user

    Returns:
        Success status
    """
    try:
        result = checkpoint_service.rollback_to_checkpoint(checkpoint_id, report=True)
        success = bool(result.get("success"))

        if success:
            # Mark checkpoint as rejected (user rolled back to it)
            checkpoint_service.mark_checkpoint_rejected(checkpoint_id)

        # `platform_files_reapplied` lets the History panel tell the customer that the
        # restore also moved platform files and that we put them back — otherwise the
        # extra commit in their history looks like it came from nowhere.
        return JSONResponse(content={
            "success": success,
            "platform_files_reapplied": result.get("platform_files_reapplied", []),
        })

    except Exception as e:
        logger.error(f"Failed to rollback checkpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/checkpoints/{checkpoint_id}/accept")
def accept_checkpoint(
    checkpoint_id: str
):
    """Mark a checkpoint as accepted by the user.

    Args:
        checkpoint_id: Git commit SHA
        current_user: Authenticated user

    Returns:
        Success status
    """
    try:
        success = checkpoint_service.mark_checkpoint_accepted(checkpoint_id)
        return JSONResponse(content={"success": success})

    except Exception as e:
        logger.error(f"Failed to accept checkpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/checkpoints/current-changes")
def get_current_changes(
    since_checkpoint: Optional[str] = None
):
    """Get list of files changed since a checkpoint (or uncommitted changes).

    Args:
        since_checkpoint: Optional git commit SHA to compare against
        current_user: Authenticated user

    Returns:
        List of changed file paths
    """
    try:
        changed_files = checkpoint_service.get_changed_files(since_checkpoint)
        return JSONResponse(content={"changed_files": changed_files})

    except Exception as e:
        logger.error(f"Failed to get current changes: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/checkpoints/current-head")
def get_current_head():
    """Get the current git HEAD commit SHA.

    Returns:
        Current HEAD commit SHA
    """
    try:
        head_sha = checkpoint_service.get_current_head()
        return JSONResponse(content={"head_sha": head_sha})

    except Exception as e:
        logger.error(f"Failed to get current HEAD: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/checkpoints/git-graph")
def get_git_graph(limit: int = 50):
    """Get commit history with branch topology for visualization.

    Returns commit data with lane assignments for rendering a git graph
    similar to SourceTree/GitKraken.

    Args:
        limit: Maximum number of commits to return (default 50)

    Returns:
        Dictionary with commits, branches, and max_branch_index
    """
    try:
        graph_data = checkpoint_service.get_git_graph(limit=limit)
        return JSONResponse(content=graph_data)

    except Exception as e:
        logger.error(f"Failed to get git graph: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/checkpoints/uncommitted")
def get_uncommitted_changes():
    """Check for uncommitted changes in the working directory.

    Returns:
        Dict with has_changes flag and list of changed/untracked files
    """
    try:
        changes = checkpoint_service.get_uncommitted_changes()
        return JSONResponse(content=changes)

    except Exception as e:
        logger.error(f"Failed to check uncommitted changes: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/checkpoints/discard")
def discard_uncommitted_changes():
    """Discard all uncommitted changes (reset to last commit).

    A snapshot is taken first, so this is undoable via /api/checkpoints/discard/undo.
    If the snapshot can't be written, nothing is discarded.

    Returns:
        Success status, count of discarded files, and the backup ref to undo with
    """
    try:
        result = checkpoint_service.discard_uncommitted_changes()
        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Failed to discard changes: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/checkpoints/discard/backups")
def list_discard_backups():
    """List the snapshots taken by past discards, newest first.

    Returns:
        Dict with the backup list, so the UI knows whether an undo is available
    """
    try:
        backups = checkpoint_service.list_discard_backups()
        return JSONResponse(content={"backups": backups, "has_backups": bool(backups)})

    except Exception as e:
        logger.error(f"Failed to list discard backups: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/checkpoints/discard/undo")
def undo_discard(request: UndoDiscardRequest = UndoDiscardRequest()):
    """Restore the changes a discard threw away.

    Returns:
        Success status, a human-readable message, and the restored file list
    """
    try:
        result = checkpoint_service.restore_discarded_changes(request.backup_ref)
        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Failed to undo discard: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/git/push")
def git_push():
    """Push commits to GitHub.

    Returns:
        Success status and git output message
    """
    import subprocess
    from pathlib import Path

    LEONARDO_PATH = Path("/app/leonardo")

    try:
        result = subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "push"],
            capture_output=True,
            text=True,
            timeout=60
        )

        success = result.returncode == 0
        message = result.stdout.strip() if success else result.stderr.strip()

        # Provide a friendlier message
        if success:
            message = "Successfully pushed to Github"

        return JSONResponse(content={
            "success": success,
            "message": message
        })

    except subprocess.TimeoutExpired:
        logger.error("Git push timed out")
        raise HTTPException(status_code=500, detail="Git push timed out after 60 seconds")
    except Exception as e:
        logger.error(f"Failed to push to GitHub: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============== Git Pull / Update Endpoints ==============
#
# These power the "pull updates" button next to the push button: an operator on
# one box (e.g. production) pulls code that was pushed from another box (e.g. a
# dev instance) and the llamapress container restarts so the new code goes live.


class CheckoutRequest(BaseModel):
    branch: str


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a git command against the Leonardo repo. Args are passed as a list
    (no shell), so values like branch names can't be interpreted as flags/shell."""
    return subprocess.run(
        ["git", "-C", str(LEONARDO_PATH), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _current_branch() -> str:
    result = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    return result.stdout.strip() if result.returncode == 0 else ""


def _remote_branches() -> list[str]:
    """List branch names available on origin (without the 'origin/' prefix)."""
    result = _git("branch", "-r", "--format=%(refname:short)", timeout=10)
    if result.returncode != 0:
        return []
    branches = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if not name or "->" in name:  # skip 'origin/HEAD -> origin/main'
            continue
        if name.startswith("origin/"):
            name = name[len("origin/"):]
        branches.append(name)
    # De-dupe while preserving order
    return list(dict.fromkeys(branches))


def _restart_llamapress() -> tuple[bool, str]:
    """Restart the llamapress (Rails) container via the Docker socket.

    Mirrors the agent's hard_restart_rails tool: resolve the container name
    dynamically (it varies by environment) and POST to the restart endpoint.
    Returns (success, message).
    """
    container_name = None
    try:
        listing = subprocess.run(
            ["curl", "--silent", "--unix-socket", "/var/run/docker.sock",
             "http://localhost/containers/json"],
            capture_output=True, text=True, timeout=5,
        )
        if listing.returncode == 0:
            for container in json.loads(listing.stdout or "[]"):
                for name in container.get("Names", []):
                    clean = name.lstrip("/").lower()
                    if "llamapress" in clean and "llamabot" not in clean:
                        container_name = name.lstrip("/")
                        break
                if container_name:
                    break
    except Exception as e:
        logger.warning(f"Could not list containers to find llamapress: {e}")

    if not container_name:
        return False, "Updated, but could not find the LlamaPress container to restart."

    try:
        restart = subprocess.run(
            ["curl", "--silent", "--show-error", "--fail-with-body", "-X", "POST",
             "--unix-socket", "/var/run/docker.sock",
             f"http://localhost/containers/{container_name}/restart?t=10"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return False, f"Updated, but the restart call failed: {e}"

    if restart.returncode != 0:
        return False, (
            "Updated, but the LlamaPress restart failed: "
            f"{(restart.stderr or restart.stdout or '').strip()}"
        )
    return True, "Update applied and LlamaPress is restarting."


@router.get("/api/git/remote-status")
def git_remote_status():
    """Report whether this box is behind the remote, plus the available branches.

    The pull button uses `behind` to show an "updates available" badge, and
    `branches` to populate the branch switcher.
    """
    try:
        fetch = _git("fetch", "--quiet")
        if fetch.returncode != 0:
            return JSONResponse(content={
                "success": False,
                "message": fetch.stderr.strip() or "git fetch failed",
            })

        branch = _current_branch()

        # Resolve the upstream ref; fall back to origin/<branch> if none is set.
        upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=10)
        ref = upstream.stdout.strip() if upstream.returncode == 0 else f"origin/{branch}"

        behind = ahead = 0
        counts = _git("rev-list", "--left-right", "--count", f"HEAD...{ref}", timeout=10)
        if counts.returncode == 0 and counts.stdout.strip():
            parts = counts.stdout.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])

        return JSONResponse(content={
            "success": True,
            "branch": branch,
            "behind": behind,
            "ahead": ahead,
            "up_to_date": behind == 0,
            "branches": _remote_branches(),
        })
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Checking for updates timed out")
    except Exception as e:
        logger.error(f"Failed to check remote status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/git/pull")
def git_pull():
    """Pull the latest code for the current branch, then restart LlamaPress.

    Uses --ff-only so a box with conflicting local edits fails loudly with a
    plain-language message instead of silently creating merge conflicts.
    """
    try:
        result = _git("pull", "--ff-only")

        if result.returncode != 0:
            # Almost always: local changes block the fast-forward. Give the
            # operator a clear, actionable message rather than raw git output.
            logger.info(f"git pull --ff-only refused: {result.stderr.strip()}")
            return JSONResponse(content={
                "success": False,
                "message": FF_BLOCKED_MESSAGE,
                "detail": result.stderr.strip(),
            })

        restarted, restart_message = _restart_llamapress()
        return JSONResponse(content={
            "success": True,
            "restarted": restarted,
            "message": restart_message if restarted else (
                "Update pulled. " + restart_message
            ),
        })
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Git pull timed out after 60 seconds")
    except Exception as e:
        logger.error(f"Failed to pull updates: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/git/checkout")
def git_checkout(request: CheckoutRequest):
    """Switch to a branch from the remote, pull its latest code, and restart.

    The branch is validated against the actual remote branch list, so an
    arbitrary/empty value can't be passed through to git.
    """
    branch = (request.branch or "").strip()
    if branch not in _remote_branches():
        raise HTTPException(status_code=400, detail=f"Unknown branch: {branch or '(empty)'}")

    try:
        checkout = _git("checkout", branch)
        if checkout.returncode != 0:
            # A dirty tree blocks the switch — same actionable message as pull.
            logger.info(f"git checkout {branch} refused: {checkout.stderr.strip()}")
            return JSONResponse(content={
                "success": False,
                "message": FF_BLOCKED_MESSAGE,
                "detail": checkout.stderr.strip(),
            })

        pull = _git("pull", "--ff-only")
        if pull.returncode != 0:
            return JSONResponse(content={
                "success": False,
                "message": FF_BLOCKED_MESSAGE,
                "detail": pull.stderr.strip(),
            })

        restarted, restart_message = _restart_llamapress()
        return JSONResponse(content={
            "success": True,
            "branch": branch,
            "restarted": restarted,
            "message": (
                f"Switched to '{branch}'. {restart_message}"
            ),
        })
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Git checkout timed out")
    except Exception as e:
        logger.error(f"Failed to checkout branch {branch}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
