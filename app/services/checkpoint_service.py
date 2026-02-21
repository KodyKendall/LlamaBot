"""Git-based checkpoint service for code rollback functionality."""
import subprocess
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path

from app.models import CheckpointInfo
from app.db import engine
from sqlmodel import Session


# Path to Leonardo repo (mounted in container)
LEONARDO_PATH = Path("/app/leonardo")


class CheckpointService:
    """Service for managing git-based checkpoints for code rollback."""

    @staticmethod
    def create_checkpoint(thread_id: str, description: str) -> Dict[str, Any]:
        """Create a git checkpoint (commit) before AI agent makes changes.

        Args:
            thread_id: The conversation thread ID
            description: Human-readable description of what's about to change

        Returns:
            Dictionary with checkpoint info including commit SHA

        Raises:
            Exception: If git operations fail
        """
        try:
            # Stage all changes
            add_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "add", "."],
                capture_output=True,
                text=True,
                timeout=10
            )

            if add_result.returncode != 0:
                raise Exception(f"Git add failed: {add_result.stderr}")

            # Create commit with standardized message format
            timestamp = datetime.now(timezone.utc).isoformat()
            commit_message = f"""🔖 Checkpoint: {description}

Thread: {thread_id}
Agent: Leonardo
Timestamp: {timestamp}
"""

            commit_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "commit", "-m", commit_message, "--allow-empty"],
                capture_output=True,
                text=True,
                timeout=10
            )

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
    def rollback_to_checkpoint(checkpoint_id: str) -> bool:
        """Rollback to a specific checkpoint (hard reset).

        Args:
            checkpoint_id: Git commit SHA to rollback to

        Returns:
            True if successful

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

            # Hard reset to checkpoint
            reset_result = subprocess.run(
                ["git", "-C", str(LEONARDO_PATH), "reset", "--hard", checkpoint_id],
                capture_output=True,
                text=True,
                timeout=10
            )

            if reset_result.returncode != 0:
                raise Exception(f"Git reset failed: {reset_result.stderr}")

            # Clean untracked files (optional, commented out for safety)
            # clean_result = subprocess.run(
            #     ["git", "-C", str(LEONARDO_PATH), "clean", "-fd"],
            #     capture_output=True,
            #     text=True,
            #     timeout=10
            # )

            return True

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to rollback: {str(e)}")

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


# Singleton instance
checkpoint_service = CheckpointService()
