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
SEED_TIMEOUT = 60

#: Where the editor container keeps its SSH state (inside the `code_config`
#: volume, so a fresh volume means a fresh, empty directory).
VSCODE_SSH_DIR = "/config/.ssh"
VSCODE_SSH_CONFIG = f"{VSCODE_SSH_DIR}/config"

#: Leonardo's directory is bind-mounted into the editor at /config/workspace,
#: so the setup script that writes the SSH config is already in the container.
VSCODE_SETUP_SCRIPT = "/config/workspace/bin/install/setup-ssh-for-lxd-vscode-container.sh"

#: The jump key every child VM on a node shares, as seen from the host.
SHARED_JUMP_KEY = ".ssh-shared/id_ed25519_lxd_jump"

#: The per-VM key the setup script generates, as seen inside the container.
CHILD_KEY = f"{VSCODE_SSH_DIR}/id_ed25519_leonardo"

#: Written with an explicit path, never `~` — see :func:`_seed_leonardo_ssh`.
HOST_AUTHORIZED_KEYS = "/home/ubuntu/.ssh/authorized_keys"


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


def _is_lxd_box() -> bool:
    """True when Leonardo's .env carries an LXD_HOST_IP.

    Only an LXD child VM needs the ProxyJump, and only its .env has the four
    LXD_* values the setup script reads. LlamaBot's own container is not given
    them — compose passes them to the editor container only — so this reads the
    file on the host.
    """
    probe = _run(
        f"grep -s '^LXD_HOST_IP=' {HOST_LEONARDO_PATH}/.env | head -1",
        INSPECT_TIMEOUT,
    )
    if not probe["ok"]:
        return False
    value = probe["output"].split("=", 1)[-1].strip().strip("\"'")
    return bool(value)


def _seed_leonardo_ssh() -> None:
    """Write the editor container's `ssh leonardo` config if it has none.

    The editor is opt-in, so its FIRST start creates a brand-new ``code_config``
    volume: ``/config/.ssh`` exists but is empty, ``ssh`` treats ``leonardo`` as
    a literal hostname, and the user gets "Could not resolve hostname leonardo"
    — which reads like a network fault and is not one.

    Guarded on the missing config file, so this is a no-op on every box that is
    already set up, and a box that is already broken repairs itself the next
    time the editor is toggled off and on.

    Never raises: a failure here must not stop the editor from coming up.
    """
    try:
        if not _is_lxd_box():
            return  # no ProxyJump to configure (Docker Desktop, a bare VM)

        already = _run(
            _compose(f"exec -T -u abc {VSCODE_SERVICE} test -f {VSCODE_SSH_CONFIG}"),
            INSPECT_TIMEOUT,
        )
        if already["ok"]:
            return  # this container already knows how to reach Leonardo

        jump_key = f"{HOST_LEONARDO_PATH}/{SHARED_JUMP_KEY}"
        if not _run(f"test -f {jump_key}", INSPECT_TIMEOUT)["ok"]:
            # Seeding a node's shared jump key from a sibling box is a
            # mothership job; there is nothing this side can do about it.
            logger.warning(f"Editor SSH seeding skipped: no shared jump key at {jump_key}")
            return

        logger.info("Seeding the code editor container's Leonardo SSH config")

        # The setup script copies the jump key itself when it can see the
        # workspace mount, but installing it first keeps this working on a box
        # whose editor does not have Leonardo mounted at /config/workspace.
        _run(_compose(f"exec -T -u abc {VSCODE_SERVICE} mkdir -p {VSCODE_SSH_DIR}"), SEED_TIMEOUT)
        for src, dest, mode in (
            (jump_key, f"{VSCODE_SSH_DIR}/id_ed25519_lxd_jump", "600"),
            (f"{jump_key}.pub", f"{VSCODE_SSH_DIR}/id_ed25519_lxd_jump.pub", "644"),
        ):
            _run(_compose(f"cp {src} {VSCODE_SERVICE}:{dest}"), SEED_TIMEOUT)
            _run(_compose(f"exec -T -u root {VSCODE_SERVICE} chown abc:abc {dest}"), SEED_TIMEOUT)
            _run(_compose(f"exec -T -u root {VSCODE_SERVICE} chmod {mode} {dest}"), SEED_TIMEOUT)

        # The script needs no arguments: compose already passes LXD_HOST_IP,
        # LXD_HOST_PORT, LXD_HOST_USER and LEONARDO_IP into the container.
        # HOME matters — it runs as `abc`, whose home is /config.
        script = _run(
            _compose(
                f"exec -T -u abc -e HOME=/config {VSCODE_SERVICE} bash {VSCODE_SETUP_SCRIPT}"
            ),
            START_TIMEOUT,
        )
        if not script["ok"]:
            logger.warning(f"Editor SSH setup script failed: {script['output'][-500:]}")
            return

        _authorize_child_key()
    except Exception as e:
        logger.warning(f"Editor SSH seeding skipped: {e}")


def _authorize_child_key() -> None:
    """Let the fresh per-VM key in on this VM.

    ``run_host_command`` goes through ``nsenter -t 1``, so it runs as ROOT on
    the host, not as ``ubuntu``. The mothership's version of this runs as the
    node's ssh user and can say ``~/.ssh/authorized_keys``; this one cannot —
    ``~`` would be ``/root``, the key would land in the wrong file, and
    ``ssh leonardo`` would still fail while the config looked perfect. Hence
    the explicit path and the chown afterwards.
    """
    pub = _run(
        _compose(f"exec -T -u abc {VSCODE_SERVICE} cat {CHILD_KEY}.pub"),
        INSPECT_TIMEOUT,
    )
    key = pub["output"].strip().splitlines()[-1].strip() if pub["ok"] and pub["output"] else ""
    if not key.startswith("ssh-"):
        logger.warning("Editor SSH seeding: no child public key to authorize")
        return

    # Drop the previous child key for this box (the script comments them
    # " leonardo-<ip>") before appending, so toggling the editor repeatedly
    # cannot grow the file, and de-duplicate what is left.
    scratch = "/tmp/llamabot_authorized_keys"
    _run(
        f"mkdir -p /home/ubuntu/.ssh && chmod 700 /home/ubuntu/.ssh "
        f"&& touch {HOST_AUTHORIZED_KEYS} "
        f"&& grep -v ' leonardo-' {HOST_AUTHORIZED_KEYS} > {scratch} "
        f"&& printf '%s\\n' '{key}' >> {scratch} "
        f"&& awk '!seen[$0]++' {scratch} > {HOST_AUTHORIZED_KEYS} "
        f"&& rm -f {scratch} "
        f"&& chown ubuntu:ubuntu {HOST_AUTHORIZED_KEYS} "
        f"&& chmod 600 {HOST_AUTHORIZED_KEYS}",
        SEED_TIMEOUT,
    )


def start_vscode() -> dict:
    """Start the editor container. Returns ``{"ok": bool, "output": str}``.

    ``up -d`` rather than ``start`` on purpose: with the compose profile in
    place the container may not exist yet, and ``docker start`` cannot create
    one.
    """
    logger.info("Starting the code editor container")
    result = _run(_compose(f"up -d {VSCODE_SERVICE}"), START_TIMEOUT)
    if result["ok"]:
        # _seed_leonardo_ssh promises not to raise, and this guards the promise:
        # the editor is what the customer asked for, and a bug in the seeder
        # must never be the reason they do not get it.
        try:
            _seed_leonardo_ssh()
        except Exception as e:
            logger.warning(f"Editor SSH seeding skipped: {e}")
    return result


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
