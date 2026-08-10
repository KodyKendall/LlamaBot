"""Reproducer for the recurring `400 insufficient tool messages` (fingerprint
4a3f1aa848a9, 30 hits / 7 instances on 0.6.0c–0.6.0e, rails_plan_mode_agent +
deepseek-v4-flash).

Two facts from the production traceback pin this down:

1. The frame is `agent_factory.py:108` — the `request.override(messages=...)`
   branch of `RepairOrphanedToolCallsMiddleware`, only reached when
   `any_repaired` is True. So the repair RAN and rewrote the history.
2. The provider rejected the result anyway.

So the bug is not "an orphan went unrepaired". The existing tests
(test_orphaned_toolcall_repair_all_agents.py, test_invalid_tool_calls_orphan.py)
all assert on the in-memory message list, which looks fine in the failing case.
The invariant they miss is what the SERIALIZER puts on the wire:

    every tool_call_id in the serialized assistant message must be answered by a
    ToolMessage that repair emitted.

`_drop_idless_tool_calls` breaks exactly that invariant. It drops id-less calls
with `model_copy`, which does NOT re-run AIMessage's validators, so when every
parsed call was id-less both `tool_calls` and `invalid_tool_calls` end up empty
— and `langchain_openai`'s serializer then falls back to
`additional_kwargs["tool_calls"]`, putting any id-bearing raw call on the wire
with nothing to answer it. `emitted_tool_calls` never reads that fallback, so
repair is blind to the orphan it just created, while the id-less drop sets
`any_repaired=True` and produces the exact traceback frame above.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.leonardo.agent_factory import (
    emitted_tool_calls,
    repair_orphaned_tool_calls_in_messages,
)


def _raw(call_id):
    """A raw OpenAI-wire tool call as it lands in `additional_kwargs`."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "bash_command", "arguments": "{}"},
    }


# --------------------------------------------------------------------------
# The invariant the provider actually enforces
# --------------------------------------------------------------------------

def assert_wire_tool_calls_are_answered(messages):
    """Raise AssertionError if `messages` would 400 with
    'insufficient tool messages following tool_calls message'.

    Asserts on the SERIALIZED payload, not the in-memory message objects — the
    whole failure mode is that those two disagree.
    """
    from langchain_openai.chat_models.base import _convert_message_to_dict

    for i, msg in enumerate(messages):
        if not isinstance(msg, AIMessage):
            continue
        wire = _convert_message_to_dict(msg)
        announced = [
            tc.get("id") for tc in (wire.get("tool_calls") or []) if tc.get("id")
        ]
        if not announced:
            continue

        answered = []
        for follower in messages[i + 1:]:
            if not isinstance(follower, ToolMessage):
                break
            answered.append(follower.tool_call_id)

        missing = [tid for tid in announced if tid not in answered]
        assert not missing, (
            f"messages[{i}] serializes tool_call_id(s) {missing} onto the wire, "
            f"but the ToolMessages following it answer {answered}. "
            f"in-memory emitted_tool_calls() sees "
            f"{[tc.get('id') for tc in emitted_tool_calls(msg)]} — which is why "
            "repair thinks this history is clean. This is the exact payload the "
            "provider rejects with 'insufficient tool messages following "
            "tool_calls message'."
        )


# --------------------------------------------------------------------------
# Confirmed reproducer: repair self-inflicts the orphan
# --------------------------------------------------------------------------

@pytest.mark.parametrize("extra_raw", [
    pytest.param([_raw("call_00_realid")], id="raw_has_one_id_bearing_call"),
    pytest.param([_raw("call_00_realid"), {**_raw("x"), "id": None}],
                 id="raw_mixes_id_bearing_and_idless"),
])
def test_dropping_idless_parsed_calls_leaves_raw_call_unanswered(extra_raw):
    """Every PARSED call is id-less, so `_drop_idless_tool_calls` empties both
    parsed lists — and the serializer falls back to the raw list, announcing a
    call that repair never answered.
    """
    msg = AIMessage(
        content="",
        tool_calls=[{"id": None, "name": "bash_command", "args": {}}],
        additional_kwargs={"tool_calls": extra_raw},
    )
    repaired = repair_orphaned_tool_calls_in_messages([
        HumanMessage(content="run the migration"),
        msg,
    ])
    assert repaired is not [HumanMessage, msg], "sanity"
    assert_wire_tool_calls_are_answered(repaired)


def test_dropping_idless_invalid_calls_leaves_raw_call_unanswered():
    """Same defect via `invalid_tool_calls` — a call whose args JSON did not
    parse AND which arrived without an id.
    """
    msg = AIMessage(
        content="",
        invalid_tool_calls=[{
            "id": None, "name": "bash_command", "args": "{unterminated",
            "error": "invalid json", "type": "invalid_tool_call",
        }],
        additional_kwargs={"tool_calls": [_raw("call_00_realid")]},
    )
    repaired = repair_orphaned_tool_calls_in_messages([
        HumanMessage(content="run the migration"),
        msg,
    ])
    assert_wire_tool_calls_are_answered(repaired)


def test_middleware_sends_a_valid_payload_for_the_failing_shape():
    """End of the real path: drive `RepairOrphanedToolCallsMiddleware` itself and
    inspect what the model would actually receive.

    This is the production code path — the middleware, not the bare helper — so
    it also pins that the middleware takes the `override(messages=...)` branch
    (the `agent_factory.py:108` frame in the incident traceback).
    """
    from app.agents.leonardo.agent_factory import RepairOrphanedToolCallsMiddleware

    class _Req:
        def __init__(self, messages):
            self.messages = messages

        def override(self, messages=None, **_):
            return _Req(messages)

    sent = {}

    async def _handler(request):
        sent["messages"] = request.messages
        return "ok"

    request = _Req([
        HumanMessage(content="run the migration"),
        AIMessage(
            content="",
            tool_calls=[{"id": None, "name": "bash_command", "args": {}}],
            additional_kwargs={"tool_calls": [_raw("call_00_realid")]},
        ),
    ])

    import asyncio
    result = asyncio.run(
        RepairOrphanedToolCallsMiddleware().awrap_model_call(request, _handler)
    )

    assert result == "ok"
    assert sent["messages"] is not request.messages, (
        "middleware must have taken the override branch"
    )
    assert_wire_tool_calls_are_answered(sent["messages"])


def test_repair_reports_it_changed_the_history_in_the_failing_case():
    """Pins fact (1): the failing path really does take the
    `request.override(messages=...)` branch, i.e. it produces the traceback
    frame we see in production rather than passing the request through."""
    msg = AIMessage(
        content="",
        tool_calls=[{"id": None, "name": "bash_command", "args": {}}],
        additional_kwargs={"tool_calls": [_raw("call_00_realid")]},
    )
    original = [HumanMessage(content="run it"), msg]
    repaired = repair_orphaned_tool_calls_in_messages(original)
    assert repaired is not original, (
        "repair must report a change (any_repaired=True) — that is the branch "
        "the production traceback lands on"
    )


# --------------------------------------------------------------------------
# Control: the shapes SI#112 already covers must stay green
# --------------------------------------------------------------------------

def test_plain_orphan_still_repairs_cleanly():
    repaired = repair_orphaned_tool_calls_in_messages([
        HumanMessage(content="run the migration"),
        AIMessage(content="", tool_calls=[
            {"id": "call_00_a", "name": "bash_command", "args": {}},
        ]),
    ])
    assert_wire_tool_calls_are_answered(repaired)


def test_idless_only_call_is_dropped_and_nothing_is_announced():
    """When the raw list has no id-bearing call either, dropping is correct and
    the wire announces nothing."""
    repaired = repair_orphaned_tool_calls_in_messages([
        HumanMessage(content="run it"),
        AIMessage(
            content="",
            tool_calls=[{"id": None, "name": "bash_command", "args": {}}],
        ),
    ])
    assert_wire_tool_calls_are_answered(repaired)


def test_additional_kwargs_only_calls_are_seen_by_repair():
    """Guard: `AIMessage`'s back-compat validator parses
    `additional_kwargs["tool_calls"]` into `.tool_calls` at CONSTRUCTION, which
    is the only reason repair sees raw-only calls at all. `model_copy` does not
    re-run that validator — which is precisely how the bug above arises. If
    upstream drops the validator, `emitted_tool_calls` must read the fallback
    itself, and this test says so.
    """
    msg = AIMessage(content="", additional_kwargs={"tool_calls": [_raw("call_raw")]})
    assert [tc["id"] for tc in emitted_tool_calls(msg)] == ["call_raw"]

    repaired = repair_orphaned_tool_calls_in_messages([
        HumanMessage(content="run it"), msg,
    ])
    assert_wire_tool_calls_are_answered(repaired)
