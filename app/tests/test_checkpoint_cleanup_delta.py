"""Tests for DeltaChannel adoption + DeltaChannel-safe checkpoint cleanup.

These assert *structure* (which channel type a state schema compiles to, which
SQL the periodic sweep runs), not LLM behavior, and need neither a real DB nor
any model credentials.
"""
import re

import pytest
from unittest.mock import AsyncMock, MagicMock

from langgraph.graph import StateGraph, START, END
from langgraph.channels.delta import DeltaChannel

from app.services.checkpoint_cleanup import (
    graph_uses_delta_channel,
    cleanup_stale_thread_checkpoints,
)


def _compile(state_schema):
    """Compile a trivial graph over `state_schema` (no LLM, no checkpointer)."""
    g = StateGraph(state_schema)
    g.add_node("noop", lambda s: {})
    g.add_edge(START, "noop")
    g.add_edge("noop", END)
    return g.compile()


# --- DeltaChannel opt-in is actually wired into the production state schemas ---

def test_rails_agent_state_messages_is_delta_channel():
    from app.agents.leonardo.rails_agent.state import RailsAgentState
    app = _compile(RailsAgentState)
    assert isinstance(app.channels["messages"], DeltaChannel), (
        "RailsAgentState.messages must be DeltaChannel-backed so the whole Rails "
        "fleet gets O(N) checkpoint storage"
    )


def test_llamabot_state_messages_is_delta_channel():
    from app.agents.llamabot.nodes import LlamaPressState
    app = _compile(LlamaPressState)
    assert isinstance(app.channels["messages"], DeltaChannel)


# --- the runtime guard that keeps cleanup correct across the mixed fleet ---

def test_graph_uses_delta_channel_true_for_delta_graph():
    from app.agents.leonardo.rails_agent.state import RailsAgentState
    app = _compile(RailsAgentState)
    assert graph_uses_delta_channel(app) is True


def test_graph_uses_delta_channel_false_for_plain_messages_graph():
    from typing import Annotated
    from typing_extensions import TypedDict
    from langgraph.graph.message import add_messages

    class PlainState(TypedDict):
        messages: Annotated[list, add_messages]

    app = _compile(PlainState)
    assert graph_uses_delta_channel(app) is False, (
        "non-delta agents must still get the old destructive post-run trim"
    )


def test_graph_uses_delta_channel_fails_safe_when_uninspectable():
    # An object with no `channels` must be treated as delta (skip destructive
    # cleanup) rather than risk corrupting a real delta thread.
    assert graph_uses_delta_channel(object()) is True


# --- periodic sweep is now orphan-only (DeltaChannel-safe), no per-thread trim ---

class _FakeResult:
    def __init__(self, row=None):
        self._row = row or (0, 0)

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, params=None):
        self.executed.append(sql)
        # First statement in cleanup_stale_thread_checkpoints is the orphan count.
        return _FakeResult((0, 0))


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        pool_conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return pool_conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_periodic_cleanup_is_orphan_only_no_destructive_trim():
    conn = _FakeConn()
    pool = _FakePool(conn)

    await cleanup_stale_thread_checkpoints(pool, stale_minutes=30)

    all_sql = "\n".join(conn.executed)

    # It must collect orphans (rows with no surviving parent checkpoint)...
    assert "checkpoint_blobs" in all_sql and "NOT EXISTS" in all_sql
    assert "checkpoint_writes" in all_sql

    # ...and must NOT do the old keep-only-latest trim, which would destroy
    # DeltaChannel delta chains.
    assert not re.search(r"MAX\(checkpoint_id\)", all_sql), (
        "periodic sweep must not trim intermediate checkpoints (breaks delta chains)"
    )
    # It must never delete from the `checkpoints` table itself (only orphan
    # blobs/writes), so whole threads stay reconstructable.
    assert not re.search(r"DELETE\s+FROM\s+checkpoints\b", all_sql, re.IGNORECASE)
