"""Who may drive the builder over MCP (``/mcp``), and whether ``/mcp`` is on at all.

LP-50. ``/mcp`` can change the customer's code, so it authenticates at least as
strictly as ``/ws`` and never falls back to "no auth" (``WS_AUTH_REQUIRED=false``
does not apply here). A bearer token is one of:

1. **Mothership token** — the format and per-box secret the mothership already
   uses for the magic-link login (``LLAMAPRESS_AI_LOGIN_SECRET``), verified by the
   same code (``magic_link_service.verify_magic_link_claims``), plus
   ``"aud": "llamabot_mcp"``::

       base64url(json({"username": <box username>, "exp": <unix s>, "aud": "llamabot_mcp"}))
         + "." + base64url(hmac_sha256(LLAMAPRESS_AI_LOGIN_SECRET, <that first part>))

   The username must be an active LlamaBot user on this box; the turn then runs
   as that user (their role, their mode grant, their threads). Lifetime is capped
   at ``MAX_TOKEN_LIFETIME_SECONDS``. A login link (no ``aud``) is NOT accepted
   here, and this token is NOT accepted as a login link.
2. **The box's own ws JWT** (``/api/ws-token``), verified by ``verify_ws_token``.
3. **A llama_bot_rails gem token**, verified by ``verify_rails_token``.

The switch: ``LLAMABOT_MCP_ENABLED`` (default on, off on a ZDR box), and the
mothership can force it off through the model policy it already pushes on
lease renewal / health report (``mcp_enabled: false``, top level or in
``instance_overrides``). Read per request, so a push takes effect at once.
"""
import hashlib
import logging
import os
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

ENABLED_ENV = "LLAMABOT_MCP_ENABLED"
RATE_LIMIT_ENV = "LLAMABOT_MCP_RATE_LIMIT_PER_MINUTE"
DEFAULT_RATE_LIMIT_PER_MINUTE = 120
#: A mothership token must expire within this long of now. Mint per use.
MAX_TOKEN_LIFETIME_SECONDS = 3600

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


# --------------------------------------------------------------------------- switch

def _policy() -> dict:
    from app.services import model_policy_store
    return model_policy_store.load()


def _zdr_enforced() -> bool:
    try:
        from app.agents.leonardo import zdr
        return zdr.enforced()
    except Exception:  # noqa: BLE001 — can't tell means treat it as ZDR
        logger.exception("MCP: could not read ZDR state; treating the box as ZDR")
        return True


def is_enabled() -> bool:
    """Is ``/mcp`` served on this box right now? Never raises; fails closed."""
    try:
        policy = _policy() or {}
        overrides = policy.get("instance_overrides")
        for source in (overrides if isinstance(overrides, dict) else {}, policy):
            if source.get("mcp_enabled") is False:
                return False
        env = (os.getenv(ENABLED_ENV) or "").strip().lower()
        if env in _FALSE:
            return False
        if env in _TRUE:
            return True
        return not _zdr_enforced()
    except Exception:  # noqa: BLE001
        logger.exception("MCP: switch check failed; serving 404")
        return False


# --------------------------------------------------------------------------- tokens

def _lookup_user(username: str):
    """The active LlamaBot user with this username, or None."""
    from sqlmodel import Session

    from app.db import engine
    from app.services.user_service import get_user_by_username

    if engine is None:
        return None
    with Session(engine) as session:
        user = get_user_by_username(session, username)
        if user is None or not user.is_active:
            return None
        return user


def principal_from_user(user) -> dict:
    """The same claims ``create_ws_token`` puts in a browser's ws JWT."""
    return {
        "sub": user.username,
        "user_id": user.id,
        "role": getattr(user, "role", "user"),
        "is_admin": bool(getattr(user, "is_admin", False)),
        "is_security_admin": getattr(user, "is_security_admin", False) is True,
        "is_system_admin": getattr(user, "is_system_admin", False) is True,
        "type": "mothership_mcp",
    }


def _verify_mothership_token(token: str) -> Optional[dict]:
    from app.services.magic_link_service import (
        MCP_AUDIENCE,
        MagicLinkError,
        verify_magic_link_claims,
    )

    try:
        claims = verify_magic_link_claims(token, audience=MCP_AUDIENCE)
    except MagicLinkError as e:
        logger.info(f"MCP: mothership token refused: {e}")
        return None
    if claims["exp"] - time.time() > MAX_TOKEN_LIFETIME_SECONDS:
        logger.info("MCP: mothership token refused: lifetime over the cap")
        return None
    try:
        user = _lookup_user(claims["username"])
    except Exception as e:  # noqa: BLE001 — fail closed
        logger.warning(f"MCP: user lookup failed, refusing: {e}")
        return None
    if user is None:
        logger.info("MCP: mothership token refused: unknown or inactive user")
        return None
    return principal_from_user(user)


def authenticate(token: Optional[str]) -> Optional[dict]:
    """The caller's identity for a bearer token, or None. Never raises."""
    if not token or not isinstance(token, str):
        return None
    try:
        dots = token.count(".")
        if dots == 2:
            from app.services.token_service import verify_ws_token
            return verify_ws_token(token)
        if dots == 1:
            return _verify_mothership_token(token)
        from app.services.token_service import is_rails_token, verify_rails_token
        if is_rails_token(token):
            return verify_rails_token(token)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MCP: token check failed, refusing: {e}")
    return None


# --------------------------------------------------------------------------- rate limit

_hits: dict = {}
_hits_lock = threading.Lock()


def reset_rate_limits() -> None:
    with _hits_lock:
        _hits.clear()


def _limit() -> int:
    try:
        return max(1, int(os.getenv(RATE_LIMIT_ENV) or DEFAULT_RATE_LIMIT_PER_MINUTE))
    except ValueError:
        return DEFAULT_RATE_LIMIT_PER_MINUTE


def allow_request(token: str) -> bool:
    """Sliding one-minute window per token. False = answer 429."""
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    limit = _limit()
    with _hits_lock:
        window = _hits.setdefault(key, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        if len(_hits) > 10_000:  # bound memory against token spraying
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                _hits.pop(k, None)
        return True
