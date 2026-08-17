"""
Repair the payload, never the past.

There are TWO repair layers for the same invariant ("every tool_call must be
answered by a ToolMessage, or the provider 400s"):

  Layer 1 — REQUEST TIME. `repair_orphaned_tool_calls_in_messages` /
    `RepairOrphanedToolCallsMiddleware` (app/agents/leonardo/agent_factory.py).
    Rebuilds the outgoing message list per model call. Nothing is persisted, and
    a structural test (test_orphaned_toolcall_repair_all_agents.py) proves all 11
    Leonardo graphs are wired for it.

  Layer 2 — STATE. `_repair_thread_state_if_needed`
    (app/websocket/request_handler.py). Rewrites the CHECKPOINT via
    `aupdate_state`.

Layer 2 is the wrong tool for this invariant, structurally: `add_messages` can
only APPEND, so a state write cannot place a ToolMessage next to the AIMessage
that owns it. The old code worked around that by deleting every message after
the offending AIMessage — buying adjacency with the user's history. Layer 1 has
no such problem because it rebuilds the list positionally.

These tests pin the resulting division of labour:

  1. Layer 2 never deletes history it does not own (the destructive workaround).
  2. Layer 2 declines the cases it cannot fix correctly, and leaves them to
     layer 1 (which fixes them on the very next model call anyway).
  3. Layer 1 covers BOTH broken shapes — including orphan ToolMessages, which
     only layer 2 used to handle. Without this, layer 2 is still load-bearing.
"""
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from app.agents.leonardo.agent_factory import repair_orphaned_tool_calls_in_messages
from app.websocket.request_handler import RequestHandler


class _App:
    """Records exactly what the repair wrote to the checkpoint."""

    def __init__(self, messages, tasks=()):
        self._messages = list(messages)
        self._tasks = list(tasks)
        self.updated_with = None

    def get_graph(self):
        return MagicMock(nodes=("__start__", "model", "tools", "__end__"))

    async def aget_state(self, config):
        snap = MagicMock()
        snap.values = {"messages": self._messages}
        snap.tasks = self._tasks
        return snap

    async def aupdate_state(self, config, update, as_node=None):
        self.updated_with = update


def _handler():
    return RequestHandler.__new__(RequestHandler)


def _removed_ids(app):
    if not app.updated_with:
        return set()
    return {
        m.id for m in app.updated_with["messages"] if isinstance(m, RemoveMessage)
    }


def _thread_that_moved_on_from_a_dead_tool_call():
    """A dangling tool_call EARLY in a thread that then carried on for a long time.

    Real cause: summarization rewrote history, or a tool crashed and the model
    continued in text. Layer 1 makes this thread perfectly sendable. The state
    repair used to delete everything from `later_1` onward.
    """
    return [
        HumanMessage(content="please run the migration", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[
            {"id": "call_dead", "name": "bash_command", "args": {}},
        ]),
        AIMessage(content="that failed, doing it by hand instead", id="later_1"),
        HumanMessage(content="ok, now add the index too", id="later_2"),
        AIMessage(content="added, here is the plan for the rest", id="later_3"),
        HumanMessage(content="great, keep going", id="later_4"),
    ]


@pytest.mark.asyncio
async def test_state_repair_never_deletes_history_it_does_not_own():
    """The bulk delete: one stale tool_call cost the user the rest of the thread."""
    app = _App(_thread_that_moved_on_from_a_dead_tool_call())
    await _handler()._repair_thread_state_if_needed(app, {"configurable": {"thread_id": "t"}})

    clobbered = _removed_ids(app) & {"later_1", "later_2", "later_3", "later_4"}
    assert not clobbered, (
        f"repair deleted {sorted(clobbered)} — messages that had nothing to do "
        "with the dangling tool_call. One stale call anywhere in a thread wipes "
        "every message after it, permanently, to buy adjacency for a synthetic "
        "ToolMessage that layer 1 would have placed correctly for free."
    )


@pytest.mark.asyncio
async def test_state_repair_declines_what_it_cannot_place_correctly():
    """An append lands at the END, which is not where the ToolMessage belongs.

    With messages after the offending AIMessage, there is no correct state-level
    fix — so layer 2 must do nothing and let layer 1 handle it.
    """
    app = _App(_thread_that_moved_on_from_a_dead_tool_call())
    await _handler()._repair_thread_state_if_needed(app, {"configurable": {"thread_id": "t"}})

    assert app.updated_with is None, (
        "layer 2 wrote a ToolMessage that cannot possibly be adjacent to its "
        f"AIMessage: {app.updated_with!r}"
    )


@pytest.mark.asyncio
async def test_state_repair_still_fixes_the_trailing_case_it_can_place():
    """When the dangling AIMessage is last, an append IS adjacent — still repair.

    This is the genuinely bricked thread (task cancelled mid-tool-execution) and
    the case the 0.7.1 `as_node` fix exists for.
    """
    app = _App([
        HumanMessage(content="build the thing", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[
            {"id": "call_1", "name": "grep_files", "args": {}},
        ]),
    ])
    await _handler()._repair_thread_state_if_needed(app, {"configurable": {"thread_id": "t"}})

    assert app.updated_with is not None, "a genuinely bricked thread must still be repaired"
    injected = [m for m in app.updated_with["messages"] if isinstance(m, ToolMessage)]
    assert injected and injected[0].tool_call_id == "call_1"
    assert not _removed_ids(app), "nothing needed deleting — the append is already adjacent"


@pytest.mark.asyncio
async def test_partially_answered_trailing_call_is_completed_without_deletion():
    """Two calls, one answered: appending the missing one keeps the block valid."""
    app = _App([
        HumanMessage(content="do both", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[
            {"id": "call_a", "name": "x", "args": {}},
            {"id": "call_b", "name": "y", "args": {}},
        ]),
        ToolMessage(content="a done", tool_call_id="call_a", id="tm_a"),
    ])
    await _handler()._repair_thread_state_if_needed(app, {"configurable": {"thread_id": "t"}})

    assert app.updated_with is not None
    injected = {m.tool_call_id for m in app.updated_with["messages"] if isinstance(m, ToolMessage)}
    assert injected == {"call_b"}
    assert "tm_a" not in _removed_ids(app), "the answer we already had must survive"


# ---------------------------------------------------------------------------
# layer 1 has to cover BOTH shapes, or layer 2 stays load-bearing
# ---------------------------------------------------------------------------

def test_request_time_repair_drops_orphan_toolmessages():
    """A ToolMessage no AIMessage ever announced 400s the provider too.

    Only the state layer handled this shape, which is why it had to keep
    rewriting checkpoints. Layer 1 must drop it from the outgoing payload so the
    thread is sendable without ever touching stored history.
    """
    msgs = [
        HumanMessage(content="hi"),
        ToolMessage(content="stale result", tool_call_id="call_ghost"),
        AIMessage(content="hello"),
    ]
    out = repair_orphaned_tool_calls_in_messages(msgs)

    assert not any(
        isinstance(m, ToolMessage) and m.tool_call_id == "call_ghost" for m in out
    ), "the orphan ToolMessage is still in the payload the provider will reject"
    assert [type(m) for m in out] == [HumanMessage, AIMessage]


def test_request_time_repair_keeps_anchored_toolmessages():
    """Only unanchored ones go — a real answer must never be dropped."""
    msgs = [
        AIMessage(content="", tool_calls=[{"id": "call_1", "name": "x", "args": {}}]),
        ToolMessage(content="the real result", tool_call_id="call_1"),
    ]
    assert repair_orphaned_tool_calls_in_messages(msgs) is msgs


def test_request_time_repair_fixes_both_shapes_at_once():
    """An orphan AND a dangling call in one thread: payload comes out valid."""
    msgs = [
        ToolMessage(content="stale", tool_call_id="call_ghost"),
        HumanMessage(content="go"),
        AIMessage(content="", tool_calls=[{"id": "call_1", "name": "x", "args": {}}]),
    ]
    out = repair_orphaned_tool_calls_in_messages(msgs)

    announced = {
        tc["id"] for m in out if isinstance(m, AIMessage) for tc in (m.tool_calls or [])
    }
    answered = {m.tool_call_id for m in out if isinstance(m, ToolMessage)}
    assert answered == announced == {"call_1"}, (
        f"payload still violates the tool-call contract: {out!r}"
    )
