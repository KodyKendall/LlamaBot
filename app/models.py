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
    # JSON array of visible agent mode keys (e.g., '["ticket", "engineer", "testing"]')
    # If null, uses default: ["ticket", "engineer", "testing", "feedback", "user"]
    visible_agents: Optional[str] = Field(default=None, max_length=500)


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


class ScheduledJob(ActiveRecordMixin, SQLModel, table=True):
    """Configuration for a scheduled agent job.

    Stores the job configuration including agent, prompt, cron schedule,
    and execution settings. Each job can be triggered by cron or manually.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=100, index=True)
    description: Optional[str] = Field(default=None, max_length=500)

    # Agent configuration
    agent_name: str = Field(max_length=50)  # e.g., "rails_agent", "llamabot"
    prompt: str = Field(max_length=10000)   # Instructions for the agent
    llm_model: str = Field(default="gemini-3-flash", max_length=50)

    # Schedule configuration (cron expression)
    cron_expression: str = Field(max_length=100)  # e.g., "0 8 * * *" (daily at 8am)
    timezone: str = Field(default="UTC", max_length=50)

    # Execution settings
    max_duration_seconds: int = Field(default=300)  # 5 min timeout
    recursion_limit: int = Field(default=100)

    # State tracking
    is_enabled: bool = Field(default=True)
    last_run_at: Optional[datetime] = Field(default=None)
    next_run_at: Optional[datetime] = Field(default=None)

    # Audit fields
    created_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScheduledJobRun(ActiveRecordMixin, SQLModel, table=True):
    """Execution record for a scheduled job run.

    Tracks each individual execution of a scheduled job, including
    timing, status, output, and token usage.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="scheduledjob.id", index=True)

    # Execution tracking
    status: str = Field(default="pending", max_length=20)  # pending/running/completed/failed/timeout
    trigger_type: str = Field(default="cron", max_length=20)  # "cron" | "manual" | "api"

    # Timing
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    duration_seconds: Optional[float] = Field(default=None)

    # LangGraph integration
    thread_id: str = Field(max_length=100, index=True)  # Links to checkpointer state

    # Output
    output_summary: Optional[str] = Field(default=None, max_length=5000)  # AI's final response
    error_message: Optional[str] = Field(default=None, max_length=2000)
    error_type: Optional[str] = Field(default=None, max_length=100)  # e.g., "TimeoutError", "AgentNotFoundError"
    error_traceback: Optional[str] = Field(default=None, max_length=5000)  # Full stack trace for debugging

    # Token usage (from LangGraph usage_metadata)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    total_tokens: int = Field(default=0)

    # Audit
    triggered_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SchedulerInvocationLog(SQLModel, table=True):
    """Log entry for each cron invocation of /api/scheduled-jobs/invoke.

    Tracks every call to the invoke endpoint, whether jobs were due or not,
    to help debug cron setup issues and monitor scheduler health.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    invoked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)

    # Request info
    source_ip: Optional[str] = Field(default=None, max_length=50)
    auth_method: str = Field(default="scheduler_token", max_length=20)  # "scheduler_token" | "user_auth"
    auth_user_id: Optional[int] = Field(default=None)

    # Result
    status: str = Field(default="success", max_length=20)  # "success" | "error" | "no_jobs_due"
    jobs_checked: int = Field(default=0)
    jobs_executed: int = Field(default=0)

    # Error details (if any)
    error_type: Optional[str] = Field(default=None, max_length=100)  # Exception class name
    error_message: Optional[str] = Field(default=None, max_length=2000)

    # Duration
    duration_ms: Optional[int] = Field(default=None)
