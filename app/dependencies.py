"""Shared FastAPI dependencies for authentication and database access."""

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlmodel import Session

from app.db import engine
from app.models import User
from app.services.user_service import authenticate_user, get_all_users

# Initialize HTTP Basic Auth
security = HTTPBasic()


def get_db_session():
    """Get database session for dependency injection."""
    with Session(engine) as session:
        yield session


def auth(
    credentials: HTTPBasicCredentials = Depends(security),
    session: Session = Depends(get_db_session)
) -> str:
    """Validate HTTP Basic Auth credentials against database."""
    user = authenticate_user(session, credentials.username, credentials.password)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return user.username


def get_current_user(
    credentials: HTTPBasicCredentials = Depends(security),
    session: Session = Depends(get_db_session)
) -> User:
    """Get the current authenticated user object."""
    user = authenticate_user(session, credentials.username, credentials.password)

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
