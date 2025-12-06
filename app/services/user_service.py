"""User service for authentication operations."""
import bcrypt
from typing import Optional
from sqlmodel import Session, select
from app.models import User


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a hash."""
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))


def get_user_by_username(session: Session, username: str) -> Optional[User]:
    """Get a user by username."""
    statement = select(User).where(User.username == username)
    return session.exec(statement).first()


def create_user(session: Session, username: str, password: str) -> User:
    """Create a new user."""
    user = User(
        username=username,
        password_hash=hash_password(password)
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def authenticate_user(session: Session, username: str, password: str) -> Optional[User]:
    """Authenticate a user and return the User object if valid."""
    user = get_user_by_username(session, username)
    if user and user.is_active and verify_password(password, user.password_hash):
        return user
    return None


def get_all_users(session: Session) -> list[User]:
    """Get all users."""
    statement = select(User).order_by(User.created_at.desc())
    return list(session.exec(statement).all())


def get_user_by_id(session: Session, user_id: int) -> Optional[User]:
    """Get a user by ID."""
    return session.get(User, user_id)


def update_user(
    session: Session,
    user_id: int,
    is_active: Optional[bool] = None,
    is_admin: Optional[bool] = None,
    new_password: Optional[str] = None,
    role: Optional[str] = None
) -> Optional[User]:
    """Update user attributes."""
    user = get_user_by_id(session, user_id)
    if not user:
        return None

    if is_active is not None:
        user.is_active = is_active
    if is_admin is not None:
        user.is_admin = is_admin
    if new_password is not None:
        user.password_hash = hash_password(new_password)
    if role is not None:
        user.role = role

    from datetime import datetime, timezone
    user.updated_at = datetime.now(timezone.utc)

    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def delete_user(session: Session, user_id: int) -> bool:
    """Delete a user by ID."""
    user = get_user_by_id(session, user_id)
    if not user:
        return False

    session.delete(user)
    session.commit()
    return True


def create_admin_user(session: Session, username: str, password: str) -> User:
    """Create a new admin user."""
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=True
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
