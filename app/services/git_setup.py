"""Set git up on demand when Save or Push fails for want of it (LP-20).

mbc-preceptors (2026-10-27): the History panel's Save failed with git's raw
"Author identity unknown" and Push with "could not read Username ...: terminal
prompts disabled". Each had a fix — /git-config, /gh-copy, /gh — but nothing on
screen said so, and /git-config used ``docker compose exec -it`` so it could not
run without a terminal anyway.

This module is that fix run for the user, in the background, at the moment it is
needed: set a missing identity, copy the host's GitHub credentials. Callers retry
the git operation ONCE afterwards; anything still failing is their job to word.
"""
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

LEONARDO_PATH = Path("/app/leonardo")

#: What /git-config sets. Only ever written where nothing is configured.
GIT_USER_NAME = "leonardo"
GIT_USER_EMAIL = "leonardo@llamapress.ai"

#: The /gh-copy sequence: host gh credentials into the llamabot and code
#: containers, then gh as git's credential helper in both. ``-T`` because nothing
#: here has a terminal. Shared with the /gh-copy slash command and the GitHub
#: sign-in flow so the three cannot drift.
GH_COPY_COMMAND = (
    "docker compose exec -T llamabot mkdir -p /root/.config/gh && "
    "docker compose exec -T code mkdir -p /config/.config/gh && "
    "docker compose cp /home/ubuntu/.config/gh/. llamabot:/root/.config/gh/ && "
    "docker compose cp /home/ubuntu/.config/gh/. code:/config/.config/gh/ && "
    "docker compose exec -T llamabot gh auth setup-git && "
    "docker compose exec -T code gh auth setup-git"
)

_MISSING_IDENTITY_MARKERS = (
    "author identity unknown",
    "committer identity unknown",
    "unable to auto-detect email address",
    "please tell me who you are",
)

_PUSH_AUTH_MARKERS = (
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "authentication failed",
    "invalid username or password",
    "permission denied",
    "error: 403",
    "returned error: 403",
)


def is_missing_identity(stderr: str) -> bool:
    """git refused to commit because it doesn't know who is committing."""
    text = (stderr or "").lower()
    return any(marker in text for marker in _MISSING_IDENTITY_MARKERS)


def is_push_auth_failure(stderr: str) -> bool:
    """git push failed for want of (working) GitHub credentials."""
    text = (stderr or "").lower()
    return any(marker in text for marker in _PUSH_AUTH_MARKERS)


def _run_on_host(command: str, timeout: int = 60):
    """Run a docker compose command from the Leonardo directory on the host."""
    from app.routers.slash_commands import execute_command

    return execute_command(command, timeout=timeout)


def ensure_git_identity() -> list:
    """Set user.name / user.email wherever they are missing. Never overwrites.

    The llamabot container is where checkpoint commits actually run, so it is
    checked against the Leonardo repo's EFFECTIVE config (a repo-local identity
    counts as configured). The code container is best effort: it is where the
    user's own terminal commits from, and it is reached through the host.

    Returns the keys set in this container, for the log.
    """
    set_here = []
    for key, value in (("user.name", GIT_USER_NAME), ("user.email", GIT_USER_EMAIL)):
        current = subprocess.run(
            ["git", "-C", str(LEONARDO_PATH), "config", "--get", key],
            capture_output=True, text=True, timeout=5,
        )
        if current.stdout.strip():
            continue
        subprocess.run(
            ["git", "config", "--global", key, value],
            capture_output=True, text=True, timeout=5,
        )
        set_here.append(key)

    code_cmd = " && ".join(
        f"docker compose exec -T code sh -c "
        f"'git config --global --get {key} >/dev/null || git config --global {key} \"{value}\"'"
        for key, value in (("user.name", GIT_USER_NAME), ("user.email", GIT_USER_EMAIL))
    )
    try:
        result = _run_on_host(code_cmd, timeout=30)
        if result.returncode != 0:
            logger.warning("Could not set the git identity in the code container: %s",
                           (result.stderr or "").strip())
    except Exception as e:  # noqa: BLE001 - the code container is a nicety here
        logger.warning("Could not set the git identity in the code container: %s", e)

    logger.info("Set missing git identity in the llamabot container: %s", set_here or "none")
    return set_here


def sync_gh_credentials() -> bool:
    """Run the /gh-copy sequence. True if it completed."""
    try:
        result = _run_on_host(GH_COPY_COMMAND, timeout=60)
    except Exception as e:  # noqa: BLE001 - the caller words the failure
        logger.warning("GitHub credential sync failed: %s", e)
        return False
    if result.returncode != 0:
        logger.warning("GitHub credential sync failed: %s", (result.stderr or "").strip())
        return False
    return True
