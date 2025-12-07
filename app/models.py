"""SQLModel database models for LlamaBot."""
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field

from app.lib import ActiveRecordMixin, set_console_session  # noqa: F401


class User(ActiveRecordMixin, SQLModel, table=True):
    """User model for authentication."""

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=50)
    password_hash: str = Field(max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = Field(default=None)
    is_active: bool = Field(default=True)
    is_admin: bool = Field(default=False)
    role: str = Field(default="engineer", max_length=20)  # engineer, user
