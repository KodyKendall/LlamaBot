"""JWT token service for WebSocket authentication and browser session cookies."""

import os
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app.models import User

logger = logging.getLogger(__name__)

# Configuration with fallbacks
SECRET_KEY = os.getenv("WS_SECRET_KEY", os.getenv("SECRET_KEY", "fallback-dev-key-change-in-production"))
EXPIRY_MINUTES = int(os.getenv("WS_TOKEN_EXPIRY_MINUTES", "30"))


def _ensure_session_secret() -> str:
    """Return SESSION_SECRET, auto-generating + persisting to .env if missing.

    Browser session cookies use a key independent from the WS token key so a
    leaked WS token cannot mint browser sessions, and vice versa.
    """
    existing = os.getenv("SESSION_SECRET")
    if existing:
        return existing
    # Lazy import to avoid a hard dep on init_pg_checkpointer at module import time
    try:
        from init_pg_checkpointer import ensure_env_variable
        return ensure_env_variable("SESSION_SECRET", secrets.token_hex(32))
    except Exception as e:
        # Fallback: generate in-memory only. Sessions won't survive restarts but
        # the app keeps working in environments where .env is read-only (CI, tests).
        logger.warning(f"Could not persist SESSION_SECRET to .env ({e}); using ephemeral key")
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
    """
    Check if a token appears to be a Rails MessageVerifier token.
    Rails tokens are base64-encoded and contain '--' separator.

    Args:
        token: Token string to check

    Returns:
        True if it looks like a Rails token
    """
    # Rails MessageVerifier tokens have format: base64_data--base64_signature
    return '--' in token and not token.startswith('eyJ')


def verify_rails_token(token: str) -> Optional[dict]:
    """
    Verify a Rails MessageVerifier token.

    Since we trust the Rails app as an internal service, we don't cryptographically
    verify the token. We just check it looks valid and extract minimal info.

    Args:
        token: Rails MessageVerifier token string

    Returns:
        Minimal payload dict if valid-looking, None otherwise
    """
    if not is_rails_token(token):
        return None

    # Trust the Rails token - return a minimal payload indicating Rails origin
    # The actual session/user info is managed by the Rails app
    return {
        "sub": "rails_gem",
        "type": "rails_auth",
        "source": "llama_bot_rails"
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
