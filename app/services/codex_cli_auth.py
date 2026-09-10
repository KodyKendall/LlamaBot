"""Device-code sign-in and token refresh, delegated to OpenAI's Codex CLI.

**Why not just call the HTTP endpoints ourselves?** We tried; see
``chatgpt_auth``. ``auth.openai.com`` is behind Cloudflare bot management that
challenges our Python client with a 403 HTML interstitial. Verified 2026-08-08
from one host, one IP, within the same minute:

  * ``codex login --device-auth``  -> issued a device code fine
  * httpx and curl to the same host -> 403 challenge page

So it is client fingerprinting, not an IP block, and no User-Agent change gets
past it (four were tried). Rather than imitate a browser's TLS fingerprint —
which is fragile and is genuinely circumventing a control — we hand the auth step
to OpenAI's own signed client, which is doing exactly what it was built for.

**Per-user isolation** comes from ``CODEX_HOME``: each user gets their own
directory, so one user's login can never read or overwrite another's. Verified:
an isolated CODEX_HOME reports "Not logged in" while the default home is logged
in.

The CLI owns *acquiring and refreshing* the credential. LlamaBot still stores it
(encrypted, per user) in ``chatgpt_credential`` so ``get_llm`` has one place to
read from — see ``sync_from_cli``.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Where per-user CODEX_HOME directories live. Must NOT be under /tmp — the CLI
# refuses to create its helper binaries there.
CODEX_STATE_ROOT = Path(
    os.getenv("CODEX_STATE_ROOT", "/app/app/.leonardo/chatgpt-auth")
)

CODEX_BIN = os.getenv("CODEX_BIN", "codex")

# The CLI prints the URL and the one-time code as separate lines; grab both
# rather than trying to match the whole (ANSI-coloured, reflowed) block.
_URL_RE = re.compile(r"https://\S*auth\.openai\.com\S*", re.I)
_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_START_TIMEOUT = 60          # seconds to wait for the CLI to print a code
_LOGIN_TIMEOUT = 15 * 60     # the CLI's own device-code lifetime

# One in-flight login per user. The process keeps running (polling OpenAI) until
# the user approves, so we hold the handle rather than the credential.
_PENDING: dict = {}


def cli_available() -> bool:
    """False when the image predates the vendored Codex CLI."""
    return shutil.which(CODEX_BIN) is not None


def codex_home_for_user(user_id: int) -> Path:
    """Isolated CODEX_HOME for one user. Created 0700."""
    home = CODEX_STATE_ROOT / str(int(user_id))
    home.mkdir(parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass
    return home


def auth_json_path(user_id: int) -> Path:
    return codex_home_for_user(user_id) / "auth.json"


def read_auth_json(user_id: int) -> Optional[dict]:
    """The CLI's stored credential for this user, or None."""
    path = auth_json_path(user_id)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _env_for(user_id: int) -> dict:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home_for_user(user_id))
    # Never let an ambient operator key turn this into an API-key login.
    env.pop("OPENAI_API_KEY", None)
    return env


class CodexCliError(Exception):
    """The CLI could not be started, or never produced a device code."""


async def start_device_login(user_id: int) -> dict:
    """Spawn ``codex login --device-auth`` and return its URL + one-time code.

    The process is left running: it polls OpenAI until the user approves, then
    writes ``auth.json`` into that user's CODEX_HOME. ``poll_device_login``
    watches for that.
    """
    if not cli_available():
        raise CodexCliError(
            "The Codex CLI is not installed in this image, so ChatGPT sign-in "
            "is unavailable. Update to a build that includes it."
        )

    await cancel_device_login(user_id)

    process = await asyncio.create_subprocess_exec(
        CODEX_BIN, "login", "--device-auth",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        env=_env_for(user_id),
        cwd=str(codex_home_for_user(user_id)),
    )

    url: Optional[str] = None
    code: Optional[str] = None
    transcript: list = []

    async def read_until_code():
        nonlocal url, code
        while True:
            raw = await process.stdout.readline()
            if not raw:
                return
            line = _strip_ansi(raw.decode("utf-8", "replace")).strip()
            if line:
                transcript.append(line)
            if url is None:
                found = _URL_RE.search(line)
                if found:
                    url = found.group(0)
            if code is None:
                found = _CODE_RE.search(line)
                if found:
                    code = found.group(1)
            if url and code:
                return

    try:
        await asyncio.wait_for(read_until_code(), timeout=_START_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        raise CodexCliError("Timed out waiting for OpenAI to issue a device code.")

    if not (url and code):
        process.kill()
        detail = " / ".join(transcript[-3:]) or "no output"
        raise CodexCliError(f"Codex CLI did not return a device code ({detail}).")

    _PENDING[user_id] = process
    logger.info("Started Codex device login for user %s", user_id)
    return {"verification_url": url, "user_code": code, "interval": 5}


async def cancel_device_login(user_id: int) -> None:
    """Kill any in-flight login for this user. Never raises."""
    process = _PENDING.pop(user_id, None)
    if process is None or process.returncode is not None:
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        return

    # Reaping is best-effort and MUST be bounded. `process.wait()` can block
    # indefinitely here: we stop reading stdout once the device code is parsed,
    # the CLI keeps writing status lines until the pipe buffer fills, and the
    # transport never reaches EOF. The process has already had SIGKILL, so a
    # missed reap costs a zombie until the worker exits — an unbounded await
    # would cost the user a hung request, which is far worse.
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        logger.debug("Codex login process for user %s did not reap in time", user_id)
    except (ProcessLookupError, OSError):
        pass
    except RuntimeError:
        # asyncio subprocess handles are bound to the loop that spawned them.
        # Under uvicorn /start and /poll share one loop so this does not arise,
        # but a caller on another loop must not turn "already killed" into a
        # 500 — the process is dead either way.
        logger.debug("Codex login process for user %s reaped on another loop", user_id)


def login_finished(user_id: int) -> bool:
    """True once the CLI has written a credential for this user."""
    blob = read_auth_json(user_id)
    return bool(blob and (blob.get("tokens") or {}).get("access_token"))


async def poll_device_login(user_id: int) -> Optional[dict]:
    """None while the user hasn't approved yet; the auth blob once they have."""
    if login_finished(user_id):
        await cancel_device_login(user_id)
        return read_auth_json(user_id)

    process = _PENDING.get(user_id)
    if process is not None and process.returncode is not None:
        # CLI exited without writing a credential — the login failed or expired.
        _PENDING.pop(user_id, None)
        raise CodexCliError("Sign-in did not complete. Start again.")
    return None


async def refresh_via_cli(
    user_id: int, *, previous_access_token: Optional[str] = None
) -> Optional[dict]:
    """Ask the CLI to refresh this user's token, and return the new blob.

    ``codex login status`` is the cheapest command that touches the credential —
    it performs no model call, so a refresh costs nothing against the user's plan.
    Returns None if the CLI is unavailable, the command failed, or the credential
    did not actually change (the caller then fails open to the operator's default
    model).

    ``previous_access_token`` is what we held BEFORE asking. Pass it: an unchanged
    token means the refresh did nothing, and reporting that as success is what
    stranded a paying customer for 62 minutes (box leo-zuset, 2026-09-09,
    agent_task 323). This function used to discard the exit code and return
    whatever auth.json already said, so a CLI that could not reach
    ``auth.openai.com`` looked identical to a successful rotation. The caller then
    re-stamped the SAME expired token with a fresh ``expires_at``, hid it for
    another hour, and sent it to chatgpt.com — which 401s with
    ``token_expired``. Meanwhile auth.json's mtime never moved: 11.3 days
    untouched, which is how the incident was finally recognised.

    A refresh that changes nothing is a FAILED refresh.
    """
    if not cli_available() or not login_finished(user_id):
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            CODEX_BIN, "login", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=_env_for(user_id),
            cwd=str(codex_home_for_user(user_id)),
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except (asyncio.TimeoutError, OSError) as e:
        logger.warning("Codex refresh for user %s failed: %s", user_id, e)
        return None

    if process.returncode != 0:
        logger.warning(
            "Codex refresh for user %s exited %s: %s",
            user_id, process.returncode,
            _strip_ansi((stdout or b"").decode("utf-8", "replace")).strip()[:400],
        )
        return None

    blob = read_auth_json(user_id)
    token = ((blob or {}).get("tokens") or {}).get("access_token")
    if not token:
        return None

    if previous_access_token and token == previous_access_token:
        # The command succeeded and rotated nothing. Treat it as no usable
        # credential so the turn falls open to the default model, instead of
        # spending another hour pretending an expired token is fresh.
        logger.warning(
            "Codex refresh for user %s returned the same access token; "
            "treating the credential as stale.", user_id,
        )
        return None

    return blob


def forget(user_id: int) -> None:
    """Delete this user's CODEX_HOME so no credential survives a disconnect."""
    home = CODEX_STATE_ROOT / str(int(user_id))
    try:
        shutil.rmtree(home)
    except (FileNotFoundError, OSError) as e:
        logger.warning("Could not remove Codex home for user %s: %s", user_id, e)
