"""Voice dictation in the chat box, backed by AssemblyAI Universal-Streaming (v3).

The browser streams microphone audio straight to AssemblyAI over a WebSocket,
so it needs a credential — but never the master key. :func:`mint_token` trades
the server-held key for a short-lived streaming token, and that token is the
only thing the browser ever sees.

Opt-in twice over: the ``enable_voice_dictation`` site setting defaults off, and
it means nothing (and cannot be switched on) while the box has no AssemblyAI key.
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SETTING_KEY = "enable_voice_dictation"

KEY_NAME = "ASSEMBLY_AI_API_KEY"

TOKEN_URL = "https://streaming.assemblyai.com/v3/token"

#: How long the browser has to OPEN the stream with the token. A session that is
#: already open keeps going past it, so this only bounds a leaked token's use.
TOKEN_TTL_SECONDS = 60


class DictationUnavailable(Exception):
    """AssemblyAI did not hand back a token. The message is safe to log, not to show."""


def api_key() -> Optional[str]:
    return (os.getenv(KEY_NAME) or "").strip() or None


def key_configured() -> bool:
    return api_key() is not None


def dictation_enabled(session) -> bool:
    from app.routers.api import get_site_setting

    try:
        return get_site_setting(session, SETTING_KEY, "false") == "true"
    except Exception as e:
        logger.warning("Could not read %s; dictation off: %s", SETTING_KEY, e)
        return False


def dictation_available(session) -> bool:
    """True only when the key is present AND the toggle is on — the mic button's gate."""
    return key_configured() and dictation_enabled(session)


async def mint_token() -> dict:
    """``{"token", "expires_in_seconds"}`` from AssemblyAI — nothing else from the reply."""
    key = api_key()
    if not key:
        raise DictationUnavailable("no AssemblyAI key configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                TOKEN_URL,
                params={"expires_in_seconds": TOKEN_TTL_SECONDS},
                headers={"Authorization": key},
            )
    except httpx.HTTPError as e:
        raise DictationUnavailable(f"AssemblyAI unreachable: {type(e).__name__}") from e

    if response.status_code != 200:
        # Status only: an error body can echo back what we sent.
        raise DictationUnavailable(f"AssemblyAI token request returned {response.status_code}")

    try:
        token = response.json().get("token")
    except Exception:
        token = None
    if not isinstance(token, str) or not token:
        raise DictationUnavailable("AssemblyAI token response had no token")

    return {"token": token, "expires_in_seconds": TOKEN_TTL_SECONDS}
