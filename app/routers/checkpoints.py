"""API routes for git-based checkpoint/rollback functionality."""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.services.checkpoint_service import checkpoint_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ============== Pydantic Models ==============

class CreateCheckpointRequest(BaseModel):
    thread_id: Optional[str] = "manual"
    description: str


class RollbackRequest(BaseModel):
    checkpoint_id: str


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
        success = checkpoint_service.rollback_to_checkpoint(checkpoint_id)

        if success:
            # Mark checkpoint as rejected (user rolled back to it)
            checkpoint_service.mark_checkpoint_rejected(checkpoint_id)

        return JSONResponse(content={"success": success})

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

    This performs git checkout -- . and git clean -fd

    Returns:
        Success status and count of discarded files
    """
    try:
        result = checkpoint_service.discard_uncommitted_changes()
        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Failed to discard changes: {str(e)}")
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
