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

    @staticmethod
    def get_uncommitted_changes() -> dict:
        """Check for uncommitted changes in the working directory.

        Returns:
            Dict with:
                - has_changes: bool
                - changed_files: list of changed file paths
                - untracked_files: list of new untracked files
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

            return {
                "has_changes": has_changes,
                "changed_files": changed_files,
                "untracked_files": untracked_files,
                "total_count": len(changed_files) + len(untracked_files)
            }

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to check for uncommitted changes: {str(e)}")

    @staticmethod
    def discard_uncommitted_changes() -> dict:
        """Discard all uncommitted changes (reset to HEAD).

        This performs:
        - git checkout -- . (discard modified files)
        - git clean -fd (remove untracked files and directories)

        Returns:
            Dict with discarded file counts
        """
        try:
            # First get what we're about to discard for reporting
            changes = CheckpointService.get_uncommitted_changes()

            if not changes["has_changes"]:
                return {
                    "success": True,
                    "message": "No changes to discard",
                    "discarded_count": 0
                }

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

            return {
                "success": True,
                "message": f"Discarded {changes['total_count']} file(s)",
                "discarded_modified": len(changes["changed_files"]),
                "discarded_untracked": len(changes["untracked_files"]),
                "discarded_count": changes["total_count"]
            }

        except subprocess.TimeoutExpired:
            raise Exception("Git operation timed out")
        except Exception as e:
            raise Exception(f"Failed to discard changes: {str(e)}")

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
