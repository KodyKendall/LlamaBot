"""Reproduction: `invalid_tool_calls` permanently brick a thread with a 400.

Symptom (reported in beginner mode AND engineer mode)::

    Error code: 400 - An assistant message with 'tool_calls' must be followed by
    tool messages responding to each 'tool_call_id'.
    (insufficient tool messages following tool_calls message)

Why the existing SI#112 repair does not catch it
------------------------------------------------
When a model emits a tool call whose ``arguments`` JSON does not parse — the
common causes are a response truncated at ``max_tokens`` in the middle of a big
``write_file`` payload, or DeepSeek emitting malformed args — LangChain files it
under ``AIMessage.invalid_tool_calls`` instead of ``AIMessage.tool_calls``.

That single split breaks the whole contract:

* LangGraph's tool router / ``ToolNode`` iterate ``.tool_calls`` only, so the
  call is never executed and no ``ToolMessage`` is ever produced for it.
* Both of our repair layers — ``repair_orphaned_tool_calls_in_messages`` (every
  graph, via ``build_leonardo_agent`` or a direct call in the raw-node agents)
  and ``RequestHandler._repair_thread_state_if_needed`` (heals state on disk) —
  also scan ``.tool_calls`` only, so they see nothing to repair.
* But ``langchain_openai``'s serializer emits ``tool_calls + invalid_tool_calls``
  into the outgoing request payload.

Net effect: the AIMessage is checkpointed carrying a tool_call_id that nothing
can ever answer, and it is re-sent on every subsequent turn. The thread 400s
forever, in every agent mode.

How these tests reproduce the 400 without a network call
--------------------------------------------------------
``test_serialized_payload_*`` asserts the exact invariant the provider enforces,
against the payload produced by the real ``langchain_openai`` serializer: every
``tool_call_id`` in an assistant message must be answered by a following tool
message. Asserting it is what the provider does when it raises the 400.

A secondary, rarer shape is covered too: a tool call carrying a null/empty
``id``. The repair explicitly skips it (``if tc.get("id") and ...``) yet it is
still serialized, as ``'id': None``.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.leonardo.agent_factory import repair_orphaned_tool_calls_in_messages
from app.websocket.request_handler import RequestHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _invalid_tool_call(id_="call_1", name="write_file"):
    """A tool call whose args JSON did not parse (truncated mid-write_file)."""
    return {
        "name": name,
        "args": '{"file_path": "app/views/posts/index.html.erb", "content": "<div',
        "id": id_,
        "error": "Function args are not valid JSON",
        "type": "invalid_tool_call",
    }


def _truncated_write_file_history():
    """The exact state a max_tokens-truncated `write_file` leaves behind."""
    return [
        HumanMessage(content="build me a posts page", id="h1"),
        AIMessage(content="", id="a1", invalid_tool_calls=[_invalid_tool_call()]),
    ]


def _unanswered_tool_call_ids(messages):
    """Tool_call_ids the PROVIDER will see unanswered, per the real serializer.

    Uses ``langchain_openai``'s own message conversion so this reflects the wire
    payload, not our in-memory view of it — the gap being reproduced is exactly
    the divergence between those two.
    """
    from langchain_openai.chat_models.base import _convert_message_to_dict

    emitted, answered = set(), set()
    for msg in messages:
        payload = _convert_message_to_dict(msg)
        if payload.get("role") == "assistant":
            for tc in payload.get("tool_calls") or []:
                emitted.add(tc.get("id"))
        elif payload.get("role") == "tool":
            answered.add(payload.get("tool_call_id"))
    return emitted - answered


# ---------------------------------------------------------------------------
# 1. The provider-facing invariant — this IS the 400
# ---------------------------------------------------------------------------

def test_serialized_payload_has_unanswered_tool_call_after_repair():
    """After the repair runs, the wire payload STILL carries an unanswerable id."""
    msgs = _truncated_write_file_history()
    repaired = repair_orphaned_tool_calls_in_messages(msgs)

    assert _unanswered_tool_call_ids(repaired) == set(), (
        "repair left a tool_call_id with no answering tool message; the provider "
        "rejects this history with 400 'insufficient tool messages following "
        "tool_calls message'"
    )


def test_serialized_payload_answers_null_id_tool_call():
    """A tool call with no id is serialized as 'id': None and never answered."""
    msgs = [
        HumanMessage(content="hi", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{"name": "read_file", "args": {}, "id": None, "type": "tool_call"}],
        ),
    ]
    repaired = repair_orphaned_tool_calls_in_messages(msgs)

    assert _unanswered_tool_call_ids(repaired) == set(), (
        "a tool_call with a null id is skipped by the repair but still emitted "
        "on the wire, so it can never be answered"
    )


# ---------------------------------------------------------------------------
# 2. Upstream cause — nothing ever produces a ToolMessage for these
# ---------------------------------------------------------------------------

def test_invalid_call_is_invisible_to_executors_but_visible_on_the_wire():
    """Documents the upstream cause — expected GREEN, and a canary if it changes.

    The whole bug in one assertion. LangGraph's tool router
    (``langchain/agents/factory.py``, ``pending_tool_calls``) and ``ToolNode``
    both iterate ``AIMessage.tool_calls``, so an invalid call is never executed
    and no ``ToolMessage`` is ever produced for it. The serializer, however,
    emits ``tool_calls + invalid_tool_calls``. That asymmetry is what makes the
    orphan permanent rather than self-healing.

    If either side ever changes upstream, this goes red and our repair should be
    re-evaluated.
    """
    from langchain_openai.chat_models.base import _convert_message_to_dict

    ai = _truncated_write_file_history()[-1]

    assert ai.tool_calls == [], "executors see nothing to run"
    assert [tc["id"] for tc in ai.invalid_tool_calls] == ["call_1"]

    wire = _convert_message_to_dict(ai)
    assert [tc["id"] for tc in wire["tool_calls"]] == ["call_1"], (
        "the provider is told a tool was called that no executor will ever answer"
    )


# ---------------------------------------------------------------------------
# 3. Repair layer 1 — the pure function (all 13 graphs)
# ---------------------------------------------------------------------------

def test_repair_function_injects_placeholder_for_invalid_tool_call():
    msgs = _truncated_write_file_history()
    out = repair_orphaned_tool_calls_in_messages(msgs)

    tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_msgs] == ["call_1"]

    # placeholder must land immediately after the AIMessage that owns the call
    ai_idx = next(i for i, m in enumerate(out) if isinstance(m, AIMessage))
    assert isinstance(out[ai_idx + 1], ToolMessage)


def test_repair_function_handles_mixed_valid_and_invalid_tool_calls():
    """One AIMessage can carry both kinds; both need answering."""
    msgs = [
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{"name": "ls", "args": {}, "id": "ok_1", "type": "tool_call"}],
            invalid_tool_calls=[_invalid_tool_call("bad_1")],
        ),
        ToolMessage(content="app/", tool_call_id="ok_1"),
    ]
    out = repair_orphaned_tool_calls_in_messages(msgs)

    ids = {m.tool_call_id for m in out if isinstance(m, ToolMessage)}
    assert ids == {"ok_1", "bad_1"}
    assert _unanswered_tool_call_ids(out) == set()


def test_repair_function_noop_when_invalid_call_already_answered():
    """Idempotent: an already-answered invalid call must not be re-injected."""
    msgs = [
        AIMessage(content="", id="a1", invalid_tool_calls=[_invalid_tool_call()]),
        ToolMessage(content="[Cancelled]", tool_call_id="call_1"),
    ]
    assert repair_orphaned_tool_calls_in_messages(msgs) is msgs


# ---------------------------------------------------------------------------
# 4. Repair layer 2 — thread state on disk (RequestHandler)
# ---------------------------------------------------------------------------

class _FakeStateSnapshot:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeApp:
    def __init__(self, messages):
        self._snapshot = _FakeStateSnapshot(messages)
        self.updated_with = None

    async def aget_state(self, config):
        return self._snapshot

    async def aupdate_state(self, config, update):
        self.updated_with = update


@pytest.mark.asyncio
async def test_state_repair_heals_a_thread_bricked_by_an_invalid_tool_call():
    """An already-bricked thread must be repaired on the next message.

    Without this the user's only recovery is starting a new thread — the repair
    layer that exists to heal cancelled-mid-tool-execution state does not see
    invalid tool calls at all.
    """
    app = _FakeApp(_truncated_write_file_history())

    await RequestHandler._repair_thread_state_if_needed(
        RequestHandler.__new__(RequestHandler), app, {"configurable": {"thread_id": "t1"}}
    )

    assert app.updated_with is not None, (
        "state repair did not fire for an AIMessage whose only tool call is an "
        "invalid_tool_call — the thread stays bricked"
    )
    injected = [
        m for m in app.updated_with["messages"]
        if isinstance(m, ToolMessage) and m.tool_call_id == "call_1"
    ]
    assert injected, "no synthetic ToolMessage was injected for the invalid tool call"
