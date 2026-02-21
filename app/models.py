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


class ThreadMetadata(ActiveRecordMixin, SQLModel, table=True):
    """Lightweight metadata for conversation threads.

    This table enables fast thread listing without loading full LangGraph checkpoint state.
    The thread_id corresponds to LangGraph's thread_id in the checkpointer.
    Stored in LEONARDO_DB_URI database (same as User model).
    """

    thread_id: str = Field(primary_key=True, max_length=100)
    title: str = Field(max_length=100, default="New Conversation")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    message_count: int = Field(default=0)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    agent_name: Optional[str] = Field(default=None, max_length=50)


class Prompt(ActiveRecordMixin, SQLModel, table=True):
    """A reusable prompt template in the global shared library.

    Prompts can be organized by group and attached to chat messages.
    All users share the same prompt library.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=100, index=True)
    content: str = Field(max_length=10000)
    description: Optional[str] = Field(default=None, max_length=500)
    group: str = Field(max_length=50, default="General", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = Field(default=None)
    is_active: bool = Field(default=True)
    usage_count: int = Field(default=0)


class CommandHistory(ActiveRecordMixin, SQLModel, table=True):
    """Stores slash command execution history."""

    id: Optional[int] = Field(default=None, primary_key=True)
    command: str = Field(max_length=50, index=True)
    args: Optional[str] = Field(default=None, max_length=1000)
    username: str = Field(max_length=50, index=True)
    success: bool
    stdout: str = Field(default="")
    stderr: str = Field(default="")
    return_code: int
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class CheckpointInfo(ActiveRecordMixin, SQLModel, table=True):
    """Git-based checkpoint for code rollback.

    Stores metadata about git commits that represent checkpoints before AI agent edits.
    Enables users to accept or reject AI changes with one-click rollback.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    checkpoint_id: str = Field(max_length=64, index=True)  # Git commit SHA
    thread_id: str = Field(max_length=100, index=True)  # Thread ID (no FK constraint - thread may not exist yet)
    description: str = Field(max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    is_accepted: Optional[bool] = Field(default=None)  # None=pending, True=accepted, False=rejected
    changed_files_count: int = Field(default=0)
