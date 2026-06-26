"""Repair path must normalize DeltaChannel `_DeltaSnapshot` message values.

SupportIncident #106: on a DeltaChannel thread, `aget_state().values["messages"]`
can surface as a `_DeltaSnapshot` whose list lives at `.value`. The repair scan
iterated that object directly, silently seeing no messages and skipping a needed
dangling-tool_call repair. These assert the normalization + that repair still
fires for a dangling tool call hidden inside a snapshot.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.websocket.request_handler import RequestHandler


class _Snapshot:
    """Stand-in for langgraph's _DeltaSnapshot: real list lives at `.value`."""
    def __init__(self, value):
        self.value = value


class TestNormalizeMessages:
    def test_unwraps_delta_snapshot(self):
        msgs = [HumanMessage(content="hi", id="h1")]
        assert RequestHandler._normalize_messages(_Snapshot(msgs)) == msgs

    def test_passthrough_plain_list(self):
        msgs = [HumanMessage(content="hi", id="h1")]
        assert RequestHandler._normalize_messages(msgs) == msgs

    def test_none_and_empty(self):
        assert RequestHandler._normalize_messages(None) == []
        assert RequestHandler._normalize_messages([]) == []

    def test_real_delta_snapshot_type_if_available(self):
        try:
            from langgraph.checkpoint.serde.types import _DeltaSnapshot
        except Exception:
            pytest.skip("_DeltaSnapshot not importable in this langgraph build")
        msgs = [HumanMessage(content="hi", id="h1")]
        snap = _DeltaSnapshot(value=msgs) if _has_kw(_DeltaSnapshot) else _DeltaSnapshot(msgs)
        assert RequestHandler._normalize_messages(snap) == msgs


def _has_kw(cls):
    import inspect
    try:
        return "value" in inspect.signature(cls).parameters
    except (ValueError, TypeError):
        return False


# --- repair fires through a snapshot-wrapped messages value ------------------

class _FakeStateSnapshot:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeApp:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.updated_with = None

    async def aget_state(self, config):
        return self._snapshot

    async def aupdate_state(self, config, update):
        self.updated_with = update


@pytest.mark.asyncio
async def test_repair_fires_for_dangling_toolcall_inside_snapshot():
    # AIMessage with a tool_call that has no following ToolMessage.
    dangling = AIMessage(
        content="",
        id="a1",
        tool_calls=[{"name": "bash_command", "id": "call_1", "args": {}}],
    )
    messages = [HumanMessage(content="do it", id="h1"), dangling]
    app = _FakeApp(_FakeStateSnapshot(_Snapshot(messages)))

    handler = RequestHandler.__new__(RequestHandler)  # skip __init__ (needs FastAPI)
    await handler._repair_thread_state_if_needed(app, {"configurable": {"thread_id": "t"}})

    assert app.updated_with is not None, "repair should have run through the snapshot"
    injected = app.updated_with["messages"]
    assert any(
        isinstance(m, ToolMessage) and m.tool_call_id == "call_1" for m in injected
    ), "a synthetic ToolMessage must be injected for the dangling tool_call"


@pytest.mark.asyncio
async def test_no_repair_for_clean_snapshot():
    messages = [
        HumanMessage(content="hi", id="h1"),
        AIMessage(content="hello", id="a1"),
    ]
    app = _FakeApp(_FakeStateSnapshot(_Snapshot(messages)))
    handler = RequestHandler.__new__(RequestHandler)
    await handler._repair_thread_state_if_needed(app, {"configurable": {"thread_id": "t"}})
    assert app.updated_with is None
