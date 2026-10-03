"""The customer's Rails app container (compose service ``llamapress``).

Custom environment variables are for the Rails app, so two questions about them
are answered HERE rather than from LlamaBot's own process:

  * :func:`live_env` — what the running Rails container actually has. LlamaBot
    loads the same ``env_file`` but is recreated on a different schedule, so its
    ``os.environ`` says nothing about whether Rails has picked a change up.
  * :func:`recreate` — apply ``.env`` changes. A ``docker restart`` keeps the
    container's old environment; only a compose recreate re-reads ``env_file``.

Neither raises: both run on request paths and the callers report the result.
"""
import json
import logging
import subprocess
from typing import Optional

from app.services.vscode_service import _run

logger = logging.getLogger(__name__)

RAILS_SERVICE = "llamapress"
RECREATE_TIMEOUT = 300

_SERVICE_LABEL = "com.docker.compose.service"
_FILES_LABEL = "com.docker.compose.project.config_files"


def _docker_get(path: str):
    result = subprocess.run(
        ["curl", "--silent", "--fail", "--unix-socket", "/var/run/docker.sock",
         f"http://localhost{path}"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout or "null")


def _find_container() -> Optional[dict]:
    """The running Rails container's listing entry, or None."""
    try:
        for container in _docker_get("/containers/json") or []:
            if (container.get("Labels") or {}).get(_SERVICE_LABEL) == RAILS_SERVICE:
                return container
    except Exception as e:
        logger.warning("Could not list containers to find Rails: %s", e)
    return None


def live_env() -> Optional[dict]:
    """``{name: value}`` of the running Rails container, or None if unreachable.

    Values never leave this process: the only caller compares fingerprints.
    """
    container = _find_container()
    if not container:
        return None
    try:
        info = _docker_get(f"/containers/{container['Id']}/json") or {}
    except Exception as e:
        logger.warning("Could not inspect the Rails container: %s", e)
        return None
    env = {}
    for entry in (info.get("Config") or {}).get("Env") or []:
        name, _, value = entry.partition("=")
        env[name] = value
    return env


def recreate() -> dict:
    """Recreate the Rails container so it loads the current ``.env``.

    Targets the compose file that created the container (its own label), so the
    dev box's ``docker-compose-dev.yml`` is honoured with no per-box config.
    ``--no-deps`` keeps postgres, redis and LlamaBot itself running.
    """
    container = _find_container()
    if not container:
        return {"ok": False, "output": "Could not find the Rails app container."}
    files = (container.get("Labels") or {}).get(_FILES_LABEL, "")
    file_flags = "".join(f"-f {f.strip()} " for f in files.split(",") if f.strip())
    if not file_flags:
        return {"ok": False, "output": "Could not tell which compose file runs the app."}
    return _run(
        f"docker compose {file_flags}up -d --force-recreate --no-deps {RAILS_SERVICE}",
        RECREATE_TIMEOUT,
    )
