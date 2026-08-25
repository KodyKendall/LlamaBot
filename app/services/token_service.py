"""JWT token service for WebSocket authentication and browser session cookies."""

import os
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app.models import User
from app.services import rails_message_verifier

# Role a verified Rails-gem caller is authorized as. The gem's token proves the
# box's own Rails app sent the frame, but a Rails user id maps to no LlamaBot
# account, so there is no per-user role to look up — the whole surface gets one
# grant, which an operator can narrow like any other role.
RAILS_ROLE = "rails"

logger = logging.getLogger(__name__)

# Configuration with fallbacks
SECRET_KEY = os.getenv("WS_SECRET_KEY", os.getenv("SECRET_KEY", "fallback-dev-key-change-in-production"))
EXPIRY_MINUTES = int(os.getenv("WS_TOKEN_EXPIRY_MINUTES", "30"))


# Key under which the durable session secret is stored in the auth DB
# (site_settings). Kept out of VALID_SITE_SETTINGS so it is never readable or
# settable through the public /api/site-settings surface — it's a secret.
SESSION_SECRET_DB_KEY = "session_secret"


def _session_secret_from_db() -> Optional[str]:
    """Read (or generate + persist) the session secret from the auth DB.

    Stored as a row in ``site_settings``. The postgres volume survives container
    recreates, so the secret is STABLE across a ``bin/update`` restart — which is
    exactly what keeps browser session cookies valid afterwards. Returns ``None``
    if the auth DB is unavailable so the caller can fall back to an ephemeral key.
    """
    try:
        from app.db import engine
        if engine is None:
            return None
        from sqlmodel import Session, SQLModel
        from app.models import SiteSetting
        # token_service can be imported before init_db() runs, so the table may
        # not exist yet — create just this one defensively (idempotent).
        try:
            SQLModel.metadata.create_all(engine, tables=[SiteSetting.__table__])
        except Exception:
            pass
        with Session(engine) as session:
            row = session.get(SiteSetting, SESSION_SECRET_DB_KEY)
            if row and row.value:
                return row.value
            new_secret = secrets.token_hex(32)
            session.add(SiteSetting(key=SESSION_SECRET_DB_KEY, value=new_secret))
            try:
                session.commit()
                return new_secret
            except Exception:
                # Lost the insert race with another worker/boot — re-read the winner.
                session.rollback()
                row = session.get(SiteSetting, SESSION_SECRET_DB_KEY)
                return row.value if row and row.value else None
    except Exception as e:
        logger.warning(f"Could not load SESSION_SECRET from auth DB: {e}")
        return None


def _ensure_session_secret() -> str:
    """Return a STABLE SESSION_SECRET that survives container recreates.

    Resolution order:
      1. ``SESSION_SECRET`` env var — explicit operator override.
      2. The auth DB (``site_settings`` row) — durable across ``bin/update``
         restarts because the postgres volume persists. Generated once, reused
         forever after.
      3. An ephemeral in-memory key — last resort when no auth DB is configured
         (CI, tests). Sessions won't survive a restart, but the app still boots.

    Browser session cookies use a key independent from the WS token key so a
    leaked WS token cannot mint browser sessions, and vice versa.

    History: this used to persist to ``.env``, but on the fleet the app's CWD
    ``.env`` lives in the container's ephemeral layer (not the host ``env_file``),
    so every ``docker compose up -d`` recreate minted a NEW secret and silently
    logged every user out. Users arrive via a LlamaPress.ai magic-link and have
    no local password, so an invalidated session locked them out entirely. The
    DB is the durable store that fixes this.
    """
    existing = os.getenv("SESSION_SECRET")
    if existing:
        return existing

    from_db = _session_secret_from_db()
    if from_db:
        return from_db

    logger.warning(
        "SESSION_SECRET not set and auth DB unavailable; using an ephemeral key — "
        "browser sessions will NOT survive a restart."
    )
    ephemeral = secrets.token_hex(32)
    os.environ["SESSION_SECRET"] = ephemeral
    return ephemeral


SESSION_SECRET = _ensure_session_secret()
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
SESSION_COOKIE_NAME = "llamabot_session"

# Cookie Secure flag — defaults to True so production is safe-by-default. Set
# LLAMABOT_COOKIE_SECURE=false in dev .env to allow plain http://localhost
# round-trips (browsers refuse to send Secure cookies over http://).
SESSION_COOKIE_SECURE = os.getenv("LLAMABOT_COOKIE_SECURE", "true").lower() != "false"


def create_session_token(user: User) -> str:
    """Sign a 30-day session JWT for browser cookie auth."""
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user.id,
        "username": user.username,
        "type": "session",
        "iat": now,
        "exp": now + timedelta(days=SESSION_TTL_DAYS),
    }
    return jwt.encode(payload, SESSION_SECRET, algorithm="HS256")


def verify_session_token(token: str) -> Optional[dict]:
    """Verify a browser session JWT. Returns the payload or None on any failure."""
    try:
        payload = jwt.decode(token, SESSION_SECRET, algorithms=["HS256"])
        if payload.get("type") != "session":
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def create_ws_token(user: User) -> str:
    """
    Generate a JWT token for WebSocket authentication.

    Args:
        user: The authenticated User object

    Returns:
        JWT token string
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.username,
        "user_id": user.id,
        "role": getattr(user, 'role', 'user'),
        "is_admin": getattr(user, 'is_admin', False),
        "type": "ws_auth",
        "iat": now,
        "exp": now + timedelta(minutes=EXPIRY_MINUTES)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def verify_ws_token(token: str) -> Optional[dict]:
    """
    Verify a JWT token and return its payload.

    Args:
        token: JWT token string

    Returns:
        Token payload dict if valid, None if invalid or expired
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        # Verify this is a WebSocket auth token
        if payload.get("type") != "ws_auth":
            logger.warning(f"Token rejected: wrong type '{payload.get('type')}'")
            return None
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Token rejected: expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Token rejected: {str(e)}")
        return None


def is_rails_token(token: str) -> bool:
    """Does this look like a Rails MessageVerifier token rather than a JWT?

    Purely a router between the two verifiers — it decides nothing about trust.
    Rails tokens are ``<base64>--<hex signature>``; JWTs are three dot-separated
    segments. Testing for the JWT shape beats the old ``startswith("eyJ")``
    check, which misread a *JSON*-serialized Rails token (its base64 also starts
    ``eyJ``) as a JWT and refused it.
    """
    if not token or not isinstance(token, str):
        return False
    if "--" not in token:
        return False
    return len(token.split(".")) != 3


def verify_rails_token(token: str) -> Optional[dict]:
    """Verify a token minted by ``llama_bot_rails`` for the chat WebSocket.

    This used to verify nothing — it returned a ``rails_auth`` payload for any
    string containing ``--``, on the stated premise that Rails is "a trusted
    internal service". Nothing established that the caller *was* Rails, so the
    premise was assumed rather than checked, and ``{"api_token": "x--y"}`` was
    enough to authenticate from the public internet and skip the agent-mode
    gate. The signature is now checked against the ``SECRET_KEY_BASE`` both
    containers share, which is what makes that premise true.

    Returns the caller's identity, or None if the token does not verify.
    """
    if not is_rails_token(token):
        return None

    secret = rails_message_verifier.secret_key_base()
    if not secret:
        # Nothing to check the signature against, so every token is
        # indistinguishable from a forgery. Refuse rather than trust.
        logger.error(
            "Rails token refused: SECRET_KEY_BASE is not set in this container, "
            "so gem tokens cannot be verified. The Rails-embedded chat needs the "
            "same SECRET_KEY_BASE as the Rails app."
        )
        return None

    try:
        payload = rails_message_verifier.verify(token, secret)
    except rails_message_verifier.InvalidRailsToken as e:
        logger.warning(f"Rails token rejected: {e}")
        return None
    except Exception as e:  # a malformed payload must not 500 the socket
        logger.warning(f"Rails token rejected (malformed): {e}")
        return None

    if not isinstance(payload, dict):
        logger.warning("Rails token rejected: payload is not a hash")
        return None

    rails_user_id = payload.get("user_id")

    # `user_id` is deliberately absent. It names a LlamaBot auth-DB user and is
    # stamped as the turn owner (request_context.set_current_user_id); a Rails
    # app's user id is a different namespace and would resolve to the wrong
    # account. The Rails id travels under its own key.
    return {
        "sub": f"rails_user:{rails_user_id}" if rails_user_id is not None else "rails_gem",
        "type": "rails_auth",
        "source": "llama_bot_rails",
        "rails_user_id": rails_user_id,
        "session_id": payload.get("session_id"),
        "role": RAILS_ROLE,
        "is_admin": False,
    }


# Scheduler token for cron-based job invocation
SCHEDULER_TOKEN = os.getenv("SCHEDULER_TOKEN")


def verify_scheduler_token(token: str) -> bool:
    """
    Verify the static scheduler token for cron invocations.

    The scheduler token is a simple static token stored in environment variables.
    It's used by cron jobs to authenticate when triggering scheduled agent runs.

    Args:
        token: Token string from X-Scheduler-Token header

    Returns:
        True if token matches SCHEDULER_TOKEN env var, False otherwise
    """
    if not SCHEDULER_TOKEN:
        logger.warning("SCHEDULER_TOKEN not configured - scheduler auth disabled")
        return False
    return token == SCHEDULER_TOKEN
