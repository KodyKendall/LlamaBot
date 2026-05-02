"""Shared FastAPI dependencies for authentication and database access.

Auth supports two paths, in priority order:

1. **Session cookie** (`llamabot_session`) — issued by `POST /login` or
   `GET /login?token=...`. Validated via PyJWT over `SESSION_SECRET`.
2. **HTTP Basic Auth** — fallback for the scheduler cron, scripted callers,
   and any browser that hasn't yet obtained a session cookie. Behavior on
   total failure is unchanged: 401 with `WWW-Authenticate: Basic`.
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlmodel import Session

from app.db import engine
from app.models import User
from app.services.user_service import (
    authenticate_user,
    get_all_users,
    get_user_by_id,
)
from app.services.token_service import SESSION_COOKIE_NAME, verify_session_token

# auto_error=False so the absence of a Basic header doesn't 401 — we want to
# try the session cookie first.
security = HTTPBasic(auto_error=False)


def get_db_session():
    """Get database session for dependency injection."""
    with Session(engine) as session:
        yield session


def _user_from_session_cookie(request: Request, session: Session) -> Optional[User]:
    """Return the User identified by a valid session cookie, or None."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    payload = verify_session_token(token)
    if not payload:
        return None
    user_id = payload.get("user_id")
    if not isinstance(user_id, int):
        return None
    user = get_user_by_id(session, user_id)
    if user and user.is_active:
        return user
    return None


def _user_from_basic_auth(
    credentials: Optional[HTTPBasicCredentials], session: Session
) -> Optional[User]:
    """Return the User identified by valid Basic Auth credentials, or None."""
    if credentials is None:
        return None
    return authenticate_user(session, credentials.username, credentials.password)


def try_authenticate(request: Request, session: Session) -> Optional[User]:
    """Best-effort auth lookup used by routes that need custom error handling
    (e.g. the `/` route, which redirects unauthenticated users in some cases).

    Tries the session cookie first, then Basic. Returns None if neither path
    succeeds; the caller decides what to do (raise 401, redirect, etc.).
    """
    user = _user_from_session_cookie(request, session)
    if user:
        return user
    # Manually parse the Authorization header — we can't call HTTPBasic from
    # outside the dependency injection system in a Request-only context.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("basic "):
        import base64
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            username, _, password = decoded.partition(":")
            return authenticate_user(session, username, password)
        except Exception:
            return None
    return None


def auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
    session: Session = Depends(get_db_session),
) -> str:
    """Validate session cookie or HTTP Basic Auth. Returns the username."""
    user = _user_from_session_cookie(request, session) or _user_from_basic_auth(
        credentials, session
    )
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return user.username


def get_current_user(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
    session: Session = Depends(get_db_session),
) -> User:
    """Validate session cookie or HTTP Basic Auth. Returns the full User."""
    user = _user_from_session_cookie(request, session) or _user_from_basic_auth(
        credentials, session
    )
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return user


def admin_required(
    current_user: User = Depends(get_current_user)
) -> User:
    """Require admin privileges."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Admin privileges required"
        )
    return current_user


def engineer_or_admin_required(
    current_user: User = Depends(get_current_user)
) -> User:
    """Require engineer or admin role for privileged operations."""
    if current_user.is_admin:
        return current_user
    if getattr(current_user, 'role', 'user') == "engineer":
        return current_user
    raise HTTPException(
        status_code=403,
        detail="Engineer or admin privileges required"
    )


def has_any_users() -> bool:
    """Check if there are any users in the database."""
    if engine is None:
        return False
    try:
        with Session(engine) as session:
            users = get_all_users(session)
            return len(users) > 0
    except Exception:
        return False
