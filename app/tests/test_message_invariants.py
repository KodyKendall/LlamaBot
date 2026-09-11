"""One validator every provider call passes through (P1-4, 2026-08-23).

Three malformed-history shapes were still killing turns on customer boxes in 30
days, and only one of them had a fix — implemented twice, once as a middleware
and once by hand inside the raw StateGraph nodes. Two implementations of one rule
means every new path starts out missing it, which is how the question-card resume
path ended up unprotected.

Also covers P1-3: our own default model returns a bare 400 with `'param': None`
(32 occurrences, 7 boxes, 7 days) and we logged nothing about the request we sent.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.leonardo.message_invariants import (
    describe_message_shape,
    log_bad_request_shape,
    looks_like_bad_request,
    normalize_messages_for_provider,
)


class TestAtLeastOneUserOrToolMessage:
    """`messages` must contain at least one message with role `user` or `tool` — 4 boxes."""

    def test_a_history_of_only_system_and_assistant_gets_one(self):
        out = normalize_messages_for_provider(
            [SystemMessage(content="you are leo"), AIMessage(content="hi")]
        )
        assert any(m.type in ("human", "tool") for m in out)

    def test_a_history_that_already_has_one_is_untouched(self):
        msgs = [SystemMessage(content="s"), HumanMessage(content="hi")]
        assert normalize_messages_for_provider(msgs) is msgs

    def test_a_tool_message_counts(self):
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "ls", "id": "c1", "args": {}}]),
            ToolMessage(content="ok", tool_call_id="c1"),
        ]
        assert normalize_messages_for_provider(msgs) is msgs

    def test_an_empty_history_is_left_alone(self):
        assert normalize_messages_for_provider([]) == []


class TestContentTypes:
    """`messages[33].content` did not match any supported type."""

    def test_an_unsupported_block_type_is_stringified_not_dropped(self):
        msgs = [HumanMessage(content=[{"type": "weird_block", "payload": {"a": 1}}])]
        out = normalize_messages_for_provider(msgs)
        blocks = out[0].content
        assert all(b["type"] == "text" for b in blocks)
        assert "weird_block" in blocks[0]["text"]

    def test_a_bare_object_in_a_content_list_becomes_text(self):
        # model_construct, because pydantic refuses to BUILD this message — which
        # is the point: the shape only ever arrives from state we did not
        # construct (a checkpoint, a provider echo), and it still has to be fixed
        # rather than raise on the way to the model.
        msgs = [HumanMessage.model_construct(content=[{"type": "text", "text": "hi"}, 42])]
        out = normalize_messages_for_provider(msgs)
        assert [b["type"] for b in out[0].content] == ["text", "text"]

    def test_supported_blocks_are_left_exactly_as_they_are(self):
        blocks = [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]
        msgs = [HumanMessage(content=list(blocks))]
        out = normalize_messages_for_provider(msgs)
        assert out[0].content == blocks

    def test_a_plain_string_content_is_left_alone(self):
        msgs = [HumanMessage(content="hello"), AIMessage(content="hi")]
        assert normalize_messages_for_provider(msgs) is msgs


class TestEmptyContent:
    def test_an_empty_user_message_is_filled_in(self):
        out = normalize_messages_for_provider([HumanMessage(content="")])
        assert out[0].content.strip()

    def test_an_assistant_turn_that_is_only_tool_calls_may_stay_empty(self):
        msgs = [
            HumanMessage(content="go"),
            AIMessage(content="", tool_calls=[{"name": "ls", "id": "c1", "args": {}}]),
            ToolMessage(content="ok", tool_call_id="c1"),
        ]
        out = normalize_messages_for_provider(msgs)
        assert out[1].content == ""

    def test_an_empty_tool_result_says_so(self):
        msgs = [
            HumanMessage(content="go"),
            AIMessage(content="", tool_calls=[{"name": "ls", "id": "c1", "args": {}}]),
            ToolMessage(content="", tool_call_id="c1"),
        ]
        out = normalize_messages_for_provider(msgs)
        assert out[2].content.strip()


class TestToolCallPairingStillWorks:
    """The rule that already had a fix must survive being moved behind one door."""

    def test_an_unanswered_tool_call_gets_a_placeholder(self):
        msgs = [
            HumanMessage(content="go"),
            AIMessage(content="", tool_calls=[{"name": "ls", "id": "c1", "args": {}}]),
        ]
        out = normalize_messages_for_provider(msgs)
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "c1" for m in out)

    def test_an_unanchored_tool_message_is_dropped(self):
        msgs = [HumanMessage(content="go"), ToolMessage(content="?", tool_call_id="nope")]
        out = normalize_messages_for_provider(msgs)
        assert not any(isinstance(m, ToolMessage) for m in out)


class TestIdempotence:
    def test_running_it_twice_changes_nothing_the_second_time(self):
        msgs = [SystemMessage(content="s"), AIMessage(content="", tool_calls=[
            {"name": "ls", "id": "c1", "args": {}}])]
        once = normalize_messages_for_provider(msgs)
        twice = normalize_messages_for_provider(once)
        assert twice is once


class TestEveryModelCallBoundaryUsesIt:
    def test_the_middleware_routes_through_the_validator(self):
        from app.agents.leonardo.agent_factory import RepairOrphanedToolCallsMiddleware

        class _Req:
            def __init__(self, messages):
                self.messages = messages
                self.overridden = None

            def override(self, messages):
                self.overridden = messages
                return self

        req = _Req([SystemMessage(content="s"), AIMessage(content="hi")])
        RepairOrphanedToolCallsMiddleware().wrap_model_call(req, lambda r: "ok")
        assert req.overridden is not None
        assert any(m.type == "human" for m in req.overridden)

    @pytest.mark.parametrize("mode", [
        "rails_beginner_agent", "rails_ai_builder_agent", "rails_plain_chat_mode",
    ])
    def test_every_raw_node_calls_it(self, mode):
        from pathlib import Path

        nodes = (Path(__file__).resolve().parents[1] / "agents" / "leonardo"
                 / mode / "nodes.py").read_text()
        assert "normalize_messages_for_provider" in nodes


# ---------------------------------------------------------------------------
# P1-3: describe the request a provider rejected
# ---------------------------------------------------------------------------

class _BadRequestError(Exception):
    pass


_BadRequestError.__name__ = "BadRequestError"


class TestBadRequestDiagnosis:
    def _history(self):
        return [
            SystemMessage(content="you are leo"),
            HumanMessage(content=[{"type": "text", "text": "hello there"}]),
            AIMessage(content="", tool_calls=[{"name": "ls", "id": "c1", "args": {}}]),
        ]

    def test_it_recognises_a_provider_400(self):
        assert looks_like_bad_request(_BadRequestError("400"))
        assert not looks_like_bad_request(ConnectionError("reset"))

    def test_the_shape_names_roles_content_kinds_and_block_types(self):
        shape = describe_message_shape(self._history(), model="muse-spark-1.2-contributor")
        roles = [m["role"] for m in shape["messages"]]
        assert roles == ["system", "human", "ai"]
        assert shape["messages"][1]["blocks"] == ["text"]
        assert shape["model"] == "muse-spark-1.2-contributor"

    def test_it_reports_tool_call_pairing(self):
        shape = describe_message_shape(self._history())
        assert shape["unanswered_tool_call_ids"] == ["c1"]

    def test_it_reports_whether_a_user_or_tool_message_is_present(self):
        assert describe_message_shape(self._history())["has_user_or_tool"] is True
        assert describe_message_shape(
            [SystemMessage(content="s"), AIMessage(content="x")]
        )["has_user_or_tool"] is False

    def test_it_reports_empty_content(self):
        shape = describe_message_shape([HumanMessage(content="")])
        assert shape["messages"][0]["empty"] is True

    def test_it_never_includes_the_actual_text(self):
        """We need the SHAPE, not the customer's content."""
        import json

        secret = "the user's private business plan"
        rendered = json.dumps(describe_message_shape([HumanMessage(content=secret)]))
        assert secret not in rendered

    def test_it_is_attached_to_the_exception_for_the_error_report(self):
        exc = _BadRequestError("400")
        log_bad_request_shape(exc, self._history(), model="muse-spark-1.2-contributor")
        assert getattr(exc, "llamabot_request_shape", None)

    def test_the_error_report_carries_it(self):
        from app.websocket.request_handler import _prepend_request_shape

        exc = _BadRequestError("400")
        log_bad_request_shape(exc, self._history())
        out = _prepend_request_shape(exc, "Traceback (most recent call last): ...")
        assert out.startswith("[llamabot request shape]")
        assert "unanswered_tool_call_ids" in out

    def test_an_ordinary_error_report_is_unchanged(self):
        from app.websocket.request_handler import _prepend_request_shape

        assert _prepend_request_shape(ValueError("x"), "tb") == "tb"


class TestResponsesApiItemsSurviveNormalization:
    """Raw ``function_call`` JSON was reaching users as assistant prose (0.7.9).

    Reported from the CRM box on a thread running Codex GPT authenticated with
    the user's own ChatGPT account. An ordinary assistant bubble read::

        Leonardo Said: {"type": "function_call", "name": "ask_user_question", ...}

    …and only THEN did the real question card render underneath it.

    On the Responses API, ``AIMessage.content`` is a list of provider ITEMS, not
    text — an assistant turn can be ``[reasoning, function_call]`` with no text
    block at all. None of those item types were in SUPPORTED_BLOCK_TYPES, so
    ``_normalize_block`` stringified each one into ``{"type": "text"}`` carrying
    its own JSON.

    Nothing is persisted — but the corrupted history is what the model READS, so
    its own prior tool calls came back to it looking like assistant prose full of
    function-call JSON. GPT-5 imitates the format it is shown, emits that JSON as
    literal text, and only then makes the real call. That is exactly the order
    the user saw.
    """

    # One assistant turn off the wire, as stored on the mothership.
    def _responses_turn(self):
        return AIMessage(content=[
            {
                "id": "rs_0c3a3741",
                "type": "reasoning",
                "summary": [{"index": 0, "type": "summary_text",
                             "text": "**Planning clarification on client links**"}],
                "content": [],
                "encrypted_content": "gAAAAABqou3-7D88aT",
            },
            {
                "type": "function_call",
                "name": "ask_user_question",
                "arguments": '{"questions":[{"question":"Want me to build this?"}]}',
                "call_id": "call_Uvjmavg53bV62DZ0wUnVG72a",
                "id": "fc_0c3a3741",
                "index": 1,
            },
        ])

    def _blocks_out(self, msg):
        out = normalize_messages_for_provider([HumanMessage(content="hi"), msg])
        return out[-1].content

    def test_no_block_becomes_text_carrying_function_call_json(self):
        """The exact defect: the tool call arriving as assistant prose."""
        blocks = self._blocks_out(self._responses_turn())

        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                assert "function_call" not in b.get("text", ""), (
                    "a function_call item was stringified into assistant text — "
                    "this is what the model then imitates back at the user"
                )

    def test_the_function_call_survives_intact(self):
        """Pass through, don't drop.

        ``function_call`` is how the Responses API expresses a call it will later
        be handed a ``function_call_output`` for. Dropping it can break the very
        "tool_calls not followed by tool messages" invariant this module exists
        to protect.
        """
        blocks = self._blocks_out(self._responses_turn())

        calls = [b for b in blocks if isinstance(b, dict) and b.get("type") == "function_call"]
        assert len(calls) == 1
        assert calls[0]["name"] == "ask_user_question"
        assert calls[0]["call_id"] == "call_Uvjmavg53bV62DZ0wUnVG72a"
        assert calls[0]["arguments"] == '{"questions":[{"question":"Want me to build this?"}]}'

    def test_the_reasoning_block_keeps_its_encrypted_content(self):
        """gpt-5.x needs ``encrypted_content`` to continue its own reasoning."""
        blocks = self._blocks_out(self._responses_turn())

        reasoning = [b for b in blocks if isinstance(b, dict) and b.get("type") == "reasoning"]
        assert len(reasoning) == 1
        assert reasoning[0]["encrypted_content"] == "gAAAAABqou3-7D88aT"

    def test_a_tool_call_only_turn_is_not_stamped_no_content(self):
        """An assistant turn whose only item is a function_call is legal, not empty."""
        msg = AIMessage(content=[{
            "type": "function_call",
            "name": "write_file",
            "arguments": "{}",
            "call_id": "call_x",
            "id": "fc_x",
        }])
        blocks = self._blocks_out(msg)

        assert blocks != "(no content)"
        if isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "text":
                    assert "(no content)" not in b.get("text", "")

    def test_an_unchanged_turn_is_returned_untouched(self):
        """No copy, no churn, when there was nothing to repair."""
        msg = self._responses_turn()
        out = normalize_messages_for_provider([HumanMessage(content="hi"), msg])
        assert out[-1] is msg

    def test_a_genuinely_unknown_block_type_is_still_stringified(self):
        """The last-resort branch stays — this is a pass-through list, not a bypass."""
        msg = AIMessage(content=[{"type": "wat_is_this", "payload": 1}])
        blocks = self._blocks_out(msg)

        assert any(
            isinstance(b, dict) and b.get("type") == "text" and "wat_is_this" in b.get("text", "")
            for b in blocks
        )

    def test_the_stringify_warning_does_not_fire_once_per_block(self, caplog):
        """40 identical WARNING lines per turn is its own bug.

        It is also what let this sit unnoticed — the signal was there, buried in
        its own volume.
        """
        import logging as _logging

        from app.agents.leonardo import message_invariants

        message_invariants._reset_block_type_warnings()
        msgs = [HumanMessage(content="hi")] + [
            AIMessage(content=[{"type": "wat_is_this", "n": i}]) for i in range(20)
        ]
        with caplog.at_level(_logging.WARNING, logger=message_invariants.__name__):
            normalize_messages_for_provider(msgs)

        hits = [r for r in caplog.records if "wat_is_this" in r.getMessage()]
        assert len(hits) == 1, f"expected one warning for the type, got {len(hits)}"
