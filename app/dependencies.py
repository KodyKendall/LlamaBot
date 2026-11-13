"""Shared FastAPI dependencies for authentication and database access.

Auth supports two paths, in priority order:

1. **Session cookie** (`llamabot_session`) — issued by `POST /login` or
   `GET /login?token=...`. Validated via PyJWT over `SESSION_SECRET`.
2. **HTTP Basic Auth** — fallback for the scheduler cron, scripted callers,
   and any browser that hasn't yet obtained a session cookie. Behavior on
   total failure is unchanged: 401 with `WWW-Authenticate: Basic`.
"""

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlmodel import Session

from app.db import engine
from app.models import AuditEvent, User
from app.permissions import holds_security_admin, holds_system_admin, is_security_only, route_allowed
from app.services.user_service import (
    authenticate_user,
    get_all_users,
    get_user_by_id,
)
from app.services.token_service import SESSION_COOKIE_NAME, verify_session_token

# auto_error=False so the absence of a Basic header doesn't 401 — we want to
# try the session cookie first.
logger = logging.getLogger(__name__)

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


def record_audit(session, event_type: str, actor=None, subject=None, detail: str = "") -> None:
    """Add to the security audit trail. Never takes the request down with it."""
    try:
        session.add(AuditEvent(event_type=event_type, actor=actor, subject=subject, detail=detail[:1000]))
        session.commit()
    except Exception as e:  # noqa: BLE001
        logger.error("Could not write audit event %s: %s", event_type, e)
        session.rollback()


def _refuse(session, user: User, request: Optional[Request], message: str) -> HTTPException:
    where = f"{request.method} {request.url.path}" if isinstance(request, Request) else "unknown route"
    logger.warning("Refused %s for %s", where, user.username)
    if isinstance(session, Session):
        record_audit(session, "access_denied", actor=user.username, detail=where)
    return HTTPException(status_code=403, detail=message)


def _check_boundary(request: Request, user: User, session: Session) -> None:
    """The separation-of-duties boundary (LP-32). Sits in the login check so that a
    login-only route is covered too, including ones added later."""
    from app.agents.leonardo import zdr

    if not route_allowed(user, request.method, request.url.path, zdr.enforced()):
        raise _refuse(session, user, request, "Your role does not include this")


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
    _check_boundary(request, user, session)
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
    _check_boundary(request, user, session)
    return user


def admin_required(
    request: Request = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> User:
    """The account owner only. Anything not classified below stays here, so it fails closed."""
    if not current_user.is_admin:
        raise _refuse(session, current_user, request, "Admin privileges required")
    return current_user


def security_admin_required(
    request: Request = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> User:
    """Users, role grants, security policy, audit log."""
    if not holds_security_admin(current_user):
        raise _refuse(session, current_user, request, "Security administrator required")
    return current_user


def system_admin_required(
    request: Request = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> User:
    """Settings, deploys, agent modes, data."""
    if not holds_system_admin(current_user):
        raise _refuse(session, current_user, request, "System administrator required")
    return current_user


def engineer_or_admin_required(
    request: Request = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
) -> User:
    """System work: a system admin, or an engineer on a box that is not institutional."""
    from app.agents.leonardo import zdr

    if holds_system_admin(current_user):
        return current_user
    if (
        not is_security_only(current_user)
        and getattr(current_user, 'role', 'user') == "engineer"
        and not zdr.enforced()
    ):
        return current_user
    raise _refuse(session, current_user, request, "Engineer or admin privileges required")


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
