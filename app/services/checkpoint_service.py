"""Git-based checkpoint service for code rollback functionality."""
import logging
import os
import subprocess
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path

from app.models import CheckpointInfo
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
