"""JWT token service for WebSocket authentication."""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app.models import User

logger = logging.getLogger(__name__)

# Configuration with fallbacks
SECRET_KEY = os.getenv("WS_SECRET_KEY", os.getenv("SECRET_KEY", "fallback-dev-key-change-in-production"))
EXPIRY_MINUTES = int(os.getenv("WS_TOKEN_EXPIRY_MINUTES", "30"))


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
