"""Runtime cache for mothership-delivered, versioned agent system prompts.

The mothership ships prompt edits over the existing ``check_updates`` round-trip
(see ``MothershipClient.check_updates``). LlamaBot stores them here, keyed by
``agent_mode`` (the LangGraph graph key), and prompt assembly adopts the cached
body for NEW agent runs.

Fail-open by contract: every accessor swallows DB errors and degrades to the
baked-in static prompt. Nothing in this module may raise — a DB hiccup must never
break the (fail-open) update check or prompt assembly.

This is the auth/app DB (``LEONARDO_DB_URI``/``AUTH_DB_URI``), NOT the
checkpointer DB.
"""
import logging
from datetime import datetime, timezone
from typing import Optional, Dict

from sqlmodel import Session, select

logger = logging.getLogger(__name__)


def get_cached(agent_mode: str) -> Optional[str]:
    """Return the cached prompt body for ``agent_mode``, or ``None``.

    Returns ``None`` on a cache miss, a missing/unconfigured DB, or ANY error —
    so callers always fall back to the static constant.
    """
    try:
        from app.db import engine
        from app.models import AgentSystemPrompt

        if engine is None:
            return None
        with Session(engine) as session:
            row = session.exec(
                select(AgentSystemPrompt).where(AgentSystemPrompt.agent_mode == agent_mode)
            ).first()
            return row.body if row else None
    except Exception as e:
        logger.warning(
            f"system_prompt_cache.get_cached({agent_mode!r}) failed; "
            f"falling back to static prompt: {e}"
        )
        return None


def cached_versions() -> Dict[str, str]:
    """Return ``{agent_mode: version}`` for every cached prompt.

    Sent to the mothership so it can return only the modes whose body changed.
    Empty dict on first boot or any error.
    """
    try:
        from app.db import engine
        from app.models import AgentSystemPrompt

        if engine is None:
            return {}
        with Session(engine) as session:
            rows = session.exec(select(AgentSystemPrompt)).all()
            return {r.agent_mode: r.version for r in rows}
    except Exception as e:
        logger.warning(f"system_prompt_cache.cached_versions failed: {e}")
        return {}


def upsert(agent_mode: str, version: str, body: str) -> None:
    """Insert or update the cached prompt for ``agent_mode``. Never raises."""
    try:
        from app.db import engine
        from app.models import AgentSystemPrompt

        if engine is None:
            return
        with Session(engine) as session:
            row = session.exec(
                select(AgentSystemPrompt).where(AgentSystemPrompt.agent_mode == agent_mode)
            ).first()
            if row:
                row.version = version
                row.body = body
                row.fetched_at = datetime.now(timezone.utc)
                session.add(row)
            else:
                session.add(
                    AgentSystemPrompt(agent_mode=agent_mode, version=version, body=body)
                )
            session.commit()
            logger.info(
                f"system_prompt_cache: cached prompt for {agent_mode!r} "
                f"(version={version}, {len(body)} chars)"
            )
    except Exception as e:
        logger.warning(f"system_prompt_cache.upsert({agent_mode!r}) failed: {e}")
