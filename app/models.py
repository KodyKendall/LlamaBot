"""SQLModel database models for LlamaBot."""
from datetime import datetime, timezone
from typing import Optional
import sqlalchemy as sa
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

    # Unified Login (llamapress.ai as identity provider). A shadow user is keyed
    # by a stable llamapress_user_guid minted by the mothership; email/display_name
    # are synced copies of the mothership profile (never a login key — see
    # app/routers/unified_login.py). NULL for legacy username/password users. The
    # unique index below is PARTIAL (only non-NULL guids collide) so any number of
    # legacy users can coexist without a guid.
    llamapress_user_guid: Optional[str] = Field(default=None, max_length=64)
    email: Optional[str] = Field(default=None, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=255)

    __table_args__ = (
        sa.Index(
            "ix_user_llamapress_user_guid",
            "llamapress_user_guid",
            unique=True,
            postgresql_where=sa.text("llamapress_user_guid IS NOT NULL"),
            sqlite_where=sa.text("llamapress_user_guid IS NOT NULL"),
        ),
    )


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
    content: str = Field(sa_type=sa.Text)  # TEXT type for unlimited length (like Rails text columns)
    description: Optional[str] = Field(default=None, max_length=500)
    group: str = Field(max_length=50, default="General", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = Field(default=None)
    is_active: bool = Field(default=True)
    usage_count: int = Field(default=0)


class AgentSystemPrompt(ActiveRecordMixin, SQLModel, table=True):
    """Mothership-delivered, versioned agent system prompt cache.

    One row per ``agent_mode`` (the LangGraph graph key). Populated at runtime
    from the ``check_updates`` round-trip so prompt edits ship without an image
    rebuild. Fail-open: absence of a row → the agent uses its baked-in static
    prompt. ``version`` is a content hash the mothership computes; LlamaBot only
    stores and echoes it back — it never hashes.
    """

    __tablename__ = "agent_system_prompts"

    id: Optional[int] = Field(default=None, primary_key=True)
    agent_mode: str = Field(max_length=100, index=True, unique=True)  # langgraph graph key
    version: str = Field(max_length=64)                               # mothership content hash
    body: str = Field(sa_type=sa.Text)                                # full prompt text
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# NOTE: The legacy DB-backed `Skill` model was removed when skills moved to the
# filesystem (.leonardo/skills/<slug>/SKILL.md — the SKILL.md open standard).
# See app/agents/leonardo/skills.py and the `use_skill` tool. The `skills` table
# is dropped by Alembic migration 20250703_000001_drop_skills_table.


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


class SiteSetting(ActiveRecordMixin, SQLModel, table=True):
    """Instance-wide key-value settings (e.g., show_token_wheel)."""

    key: str = Field(primary_key=True, max_length=100)
    value: str = Field(max_length=1000)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CustomEnvVar(ActiveRecordMixin, SQLModel, table=True):
    """An operator-defined environment variable, rendered into the instance .env.

    Deliberately NOT a ``SiteSetting`` row, for the same reason as
    ``ChatGptCredential``: ``SiteSetting.value`` is ``max_length=1000`` and these
    routinely hold connection strings and JWT-shaped service tokens. ``value`` is
    a ``Text`` column.

    The database is the source of truth; the ``.env`` managed block is a rendered
    artifact regenerated from these rows (see
    ``services/env_settings_service.sync_managed_block``). That direction matters
    — the file is wiped and rebuilt by tooling, the rows are not.

    A row here can NEVER override a variable already present in ``.env``. The
    render step drops any name that collides, and the row is surfaced in the UI
    as shadowed/inactive rather than silently winning.
    """

    __tablename__ = "custom_env_var"

    name: str = Field(primary_key=True, max_length=128)
    value: str = Field(sa_column=sa.Column(sa.Text, nullable=False))

    # Free-text note so the next operator knows why this exists.
    description: Optional[str] = Field(default=None, max_length=500)

    created_by_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatGptCredential(ActiveRecordMixin, SQLModel, table=True):
    """A user's ChatGPT (Codex) OAuth credential, for running Leo on their own plan.

    Deliberately NOT a ``SiteSetting`` row: ``SiteSetting.value`` is
    ``max_length=1000`` and OpenAI's access tokens are JWTs that exceed that on
    their own, never mind an access+refresh pair. These are ``Text`` columns.

    Scoped per ``user_id``, not per instance — an instance can have several users
    (see ``User.role``), and one user's subscription must never serve another's
    turn.

    **The tokens in this table must never leave the container.** They are not
    readable through ``/api/site-settings``, must not appear in error telemetry or
    feedback snapshots, and are never sent to the mothership. See
    ``docs/dev/chatgpt_oauth_byo_subscription.md`` §1 constraint 1.
    """

    __tablename__ = "chatgpt_credential"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", unique=True, index=True)

    # Fernet-encrypted at rest (see services/chatgpt_auth.py). Text, not String.
    access_token_encrypted: str = Field(sa_column=sa.Column(sa.Text, nullable=False))
    refresh_token_encrypted: str = Field(sa_column=sa.Column(sa.Text, nullable=False))

    account_id: Optional[str] = Field(default=None, max_length=128)
    plan_tier: Optional[str] = Field(default=None, max_length=64)
    account_email: Optional[str] = Field(default=None, max_length=255)

    expires_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_refreshed_at: Optional[datetime] = Field(default=None)

    # Set when OpenAI rejects the credential (revoked, plan lapsed, originator
    # refused). Keeps the row for UI ("reconnect") while get_llm fails open to the
    # default model instead of retrying a known-dead token every turn.
    disconnected_reason: Optional[str] = Field(default=None, max_length=255)


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
