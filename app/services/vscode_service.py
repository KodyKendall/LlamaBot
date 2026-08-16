"""The code editor toggle: one setting that owns the Code tab and the container.

The VS Code editor (code-server) runs as its own container, named ``code`` in
Leonardo's compose file. It is opt-in. A box boots with the editor stopped and
the Code tab hidden until an engineer turns the editor on in Settings.

Two guards keep the editor from coming back on its own:

1. ``profiles: ["code"]`` on the compose service, so a plain ``docker compose
   up -d`` (what a reboot and ``bin/update`` run) skips the editor container.
2. :func:`reconcile_vscode` at LlamaBot startup, which stops the container if it
   is running while the setting says off. Guard 2 covers a box whose compose
   file does not have the profile yet.

Boot never STARTS the editor. Starting the editor is always a human action, so a
box that has been enabled once still comes back with the editor stopped after a
reboot until someone asks for it. That is the point of the feature: an editor
that is not running cannot be attacked, and it does not use memory.

Docker commands run on the HOST through nsenter, because the LlamaBot container
has no docker client of its own. This module runs them from no particular
directory: :func:`app.routers.slash_commands.execute_command` changes into
``HOST_LEONARDO_PATH`` first, and that default path is wrong on any box where
Leonardo lives somewhere else, which would make every editor command fail before
docker ever ran.
"""
import asyncio
import logging
import os
import subprocess

from app.routers.slash_commands import (
    HOST_LEONARDO_PATH,
    LEONARDO_PATH,
    is_native_linux_host,
)

logger = logging.getLogger(__name__)

#: The site setting key. Stored as the string "true" or "false".
VSCODE_SETTING_KEY = "enable_vscode"

#: The compose service AND container name for code-server (both are ``code``).
VSCODE_SERVICE = "code"

#: The compose profile that keeps the editor out of a plain ``up -d``.
VSCODE_PROFILE = "code"

#: Seconds to wait for docker. Pulling an image on first start is the slow case.
START_TIMEOUT = 180
STOP_TIMEOUT = 60
INSPECT_TIMEOUT = 15


def vscode_enabled(session) -> bool:
    """True when the editor is switched on for this box.

    Fails closed: an unreadable setting means the editor stays off and the Code
    tab stays hidden, rather than a tab that points at a stopped editor.
    """
    from app.routers.api import get_site_setting

    try:
        return get_site_setting(session, VSCODE_SETTING_KEY, "false") == "true"
    except Exception as e:
        logger.warning(f"Could not read '{VSCODE_SETTING_KEY}', treating the editor as off: {e}")
        return False


def run_host_command(command: str, timeout: int) -> subprocess.CompletedProcess:
    """Run one command on the host machine.

    On Linux the command runs in the host's namespaces through nsenter, the same
    mechanism the slash commands use. Anywhere else (Docker Desktop) it runs
    inside this container, where Leonardo is mounted at ``LEONARDO_PATH``.
    """
    if is_native_linux_host():
        return subprocess.run(
            ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "/bin/bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    return subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=LEONARDO_PATH,
    )


def _compose_files() -> list[str]:
    """Which compose file(s) to pass to ``docker compose``.

    Production runs the default ``docker-compose.yml``, but the dev box runs
    ``docker-compose-dev.yml``. Targeting the wrong file would build a SECOND,
    differently configured editor container, so the file is resolved in this
    order:

    1. ``LEONARDO_COMPOSE_FILE``, one or more paths separated by commas.
    2. The label docker wrote on the existing editor container, which records
       the compose file that created it.
    3. Nothing, which lets docker compose pick its default file.
    """
    override = os.getenv("LEONARDO_COMPOSE_FILE", "").strip()
    if override:
        return [part.strip() for part in override.split(",") if part.strip()]

    label = _run(
        "docker inspect -f '{{index .Config.Labels \"com.docker.compose.project.config_files\"}}' "
        + VSCODE_SERVICE,
        INSPECT_TIMEOUT,
    )
    if not label["ok"]:
        return []

    value = label["output"].strip()
    if not value or "<no value>" in value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _compose(args: str) -> str:
    """Build one ``docker compose`` command for the editor service.

    With a compose file path the command needs no working directory. Without one
    (a box whose editor container has never been created) the command has to run
    from Leonardo's directory, so docker compose can find its default file.
    """
    files = _compose_files()
    if files:
        file_flags = "".join(f"-f {path} " for path in files)
        return f"docker compose {file_flags}--profile {VSCODE_PROFILE} {args}"
    return f"cd {HOST_LEONARDO_PATH} && docker compose --profile {VSCODE_PROFILE} {args}"


def _run(command: str, timeout: int) -> dict:
    """Run one docker command. Never raises — the caller reports the result."""
    try:
        result = run_host_command(command, timeout=timeout)
    except Exception as e:
        logger.warning(f"Editor command failed to run ({command}): {e}")
        return {"ok": False, "output": str(e)}

    output = (result.stdout or "") + (result.stderr or "")
    return {"ok": result.returncode == 0, "output": output.strip()}


def start_vscode() -> dict:
    """Start the editor container. Returns ``{"ok": bool, "output": str}``.

    ``up -d`` rather than ``start`` on purpose: with the compose profile in
    place the container may not exist yet, and ``docker start`` cannot create
    one.
    """
    logger.info("Starting the code editor container")
    return _run(_compose(f"up -d {VSCODE_SERVICE}"), START_TIMEOUT)


def stop_vscode() -> dict:
    """Stop the editor container. Returns ``{"ok": bool, "output": str}``."""
    logger.info("Stopping the code editor container")
    return _run(_compose(f"stop {VSCODE_SERVICE}"), STOP_TIMEOUT)


def vscode_running() -> bool:
    """True when the editor container exists and is running."""
    result = _run(
        "docker inspect -f '{{.State.Running}}' " + VSCODE_SERVICE,
        INSPECT_TIMEOUT,
    )
    return result["ok"] and "true" in result["output"].lower()


async def reconcile_vscode(session) -> str:
    """Make the container match the setting at startup.

    Returns "stopped", "none", or "error". Called from the startup event, so it
    must never raise into boot, and it must not block the event loop while
    docker works.
    """
    try:
        if vscode_enabled(session):
            return "none"

        if not await asyncio.to_thread(vscode_running):
            return "none"

        logger.info("Editor is off in Settings but the container is running — stopping it")
        await asyncio.to_thread(stop_vscode)
        return "stopped"
    except Exception as e:
        logger.warning(f"Editor startup check skipped: {e}")
        return "error"
