"""Tests for RailsSummarizationMiddleware + make_summarization_middleware.

Covers the user-facing retention contract: after a summarization the rebuilt
message list must contain (a) the first K user messages verbatim, (b) the
summary with the live todo list re-injected, and (c) the recent tail — and
nothing else. Also proves it composes with the DeltaChannel REMOVE_ALL reducer so
the reconstructed state actually shrinks (no loop).

No real LLM: a fake chat model returns a fixed summary string.
"""
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    RemoveMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from app.agents.leonardo.summarization import (
    POST_COMPACTION_TARGET_RATIO,
    RailsSummarizationMiddleware,
    make_summarization_middleware,
)
from app.agents.utils.delta_state import messages_delta_reducer
from app.agents.utils.token_counter import (
    SUMMARIZATION_KEEP_TOKENS,
    SUMMARIZATION_TOKEN_THRESHOLD,
)


class _FakeSummaryModel(BaseChatModel):
    """Returns a fixed summary; counts every message as 1000 'tokens' via len."""

    @property
    def _llm_type(self):
        return "fake-summary"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="FIXED SUMMARY TEXT"))]
        )


def _word_counter(messages):
    """Cheap deterministic counter: 1000 'tokens' per message."""
    return 1000 * len(list(messages))


def _mw(keep_initial_human=3):
    return RailsSummarizationMiddleware(
        model=_FakeSummaryModel(),
        trigger=("tokens", 5000),       # 5 messages
        keep=("tokens", 2000),          # ~2 messages tail
        token_counter=_word_counter,
        trim_tokens_to_summarize=None,
        summary_prompt="Summarize:\n{messages}",
        keep_initial_human=keep_initial_human,
    )


def _long_convo():
    """A realistic-ish thread with an early write_todos call."""
    return [
        HumanMessage(content="Build me a blog with posts and comments", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{
                "name": "write_todos",
                "id": "tc1",
                "args": {"todos": [
                    {"content": "scaffold Post model", "status": "completed"},
                    {"content": "add Comment model", "status": "in_progress"},
                    {"content": "wire routes", "status": "pending"},
                ]},
            }],
        ),
        ToolMessage(content="Updated todo list", tool_call_id="tc1", id="t1"),
        HumanMessage(content="also add tags", id="h2"),
        AIMessage(content="working on tags", id="a2"),
        HumanMessage(content="how's it going?", id="h3"),
        AIMessage(content="almost done", id="a3"),
    ]


class TestAugmentStructure:
    def test_preserves_first_user_message_verbatim(self):
        mw = _mw(keep_initial_human=1)
        result = mw.before_model({"messages": _long_convo()}, runtime=None)
        msgs = result["messages"]
        assert isinstance(msgs[0], RemoveMessage) and msgs[0].id == REMOVE_ALL_MESSAGES
        # first real human message, verbatim, right after the remove marker
        assert isinstance(msgs[1], HumanMessage)
        assert msgs[1].content == "Build me a blog with posts and comments"
        assert msgs[1].id == "h1"

    def test_summary_present_with_todo_restore_block(self):
        mw = _mw()
        result = mw.before_model({"messages": _long_convo()}, runtime=None)
        summary = next(
            m for m in result["messages"]
            if isinstance(m, HumanMessage)
            and (m.additional_kwargs or {}).get("lc_source") == "summarization"
        )
        assert "FIXED SUMMARY TEXT" in summary.content
        assert "ACTIVE TODO LIST" in summary.content
        assert "write_todos" in summary.content
        # the actual todo content must be embedded so the model can restore it
        assert "scaffold Post model" in summary.content
        assert "add Comment model" in summary.content

    def test_no_pre_summary_history_leaks(self):
        mw = _mw(keep_initial_human=1)
        result = mw.before_model({"messages": _long_convo()}, runtime=None)
        # The mid-conversation messages that were summarized (h2/a2/t1) must not
        # reappear verbatim outside the preserved tail.
        ids = [getattr(m, "id", None) for m in result["messages"]]
        assert "t1" not in ids  # the tool message in the middle was summarized away

    def test_reconstruction_through_delta_reducer_shrinks(self):
        """End-to-end: feed the summarize write through the real reducer."""
        convo = _long_convo()
        mw = _mw(keep_initial_human=1)
        result = mw.before_model({"messages": convo}, runtime=None)
        reconstructed = messages_delta_reducer(convo, [result["messages"]])
        assert len(reconstructed) < len(convo), "post-summary state must shrink"
        # first user message + summary survive
        assert any(m.id == "h1" for m in reconstructed)
        assert any(
            isinstance(m, HumanMessage)
            and (m.additional_kwargs or {}).get("lc_source") == "summarization"
            for m in reconstructed
        )

    def test_no_summarization_below_trigger_returns_none(self):
        mw = _mw()
        short = [HumanMessage(content="hi", id="h1"), AIMessage(content="hello", id="a1")]
        assert mw.before_model({"messages": short}, runtime=None) is None

    def test_handles_thread_with_no_todos(self):
        mw = _mw(keep_initial_human=1)
        convo = [
            HumanMessage(content="first", id="h1"),
            AIMessage(content="a", id="a1"),
            HumanMessage(content="second", id="h2"),
            AIMessage(content="b", id="a2"),
            HumanMessage(content="third", id="h3"),
            AIMessage(content="c", id="a3"),
        ]
        result = mw.before_model({"messages": convo}, runtime=None)
        summary = next(
            m for m in result["messages"]
            if isinstance(m, HumanMessage)
            and (m.additional_kwargs or {}).get("lc_source") == "summarization"
        )
        assert "ACTIVE TODO LIST" not in summary.content


class TestExtractTodos:
    def test_extracts_most_recent(self):
        msgs = [
            AIMessage(content="", id="a1", tool_calls=[{
                "name": "write_todos", "id": "x1",
                "args": {"todos": [{"content": "old", "status": "pending"}]}}]),
            AIMessage(content="", id="a2", tool_calls=[{
                "name": "write_todos", "id": "x2",
                "args": {"todos": [{"content": "new", "status": "pending"}]}}]),
        ]
        todos = RailsSummarizationMiddleware._extract_last_todos(msgs)
        assert todos == [{"content": "new", "status": "pending"}]

    def test_none_when_absent(self):
        msgs = [HumanMessage(content="hi", id="h1"), AIMessage(content="yo", id="a1")]
        assert RailsSummarizationMiddleware._extract_last_todos(msgs) is None


class TestFactory:
    def test_factory_builds_token_keyed_keep(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        mw = make_summarization_middleware(summary_prompt="Summarize {messages}")
        assert isinstance(mw, RailsSummarizationMiddleware)
        assert mw.trigger == ("tokens", SUMMARIZATION_TOKEN_THRESHOLD)
        assert mw.keep == ("tokens", SUMMARIZATION_KEEP_TOKENS)
        assert mw.keep_initial_human == 3

    def test_factory_gives_the_initial_preserve_a_token_budget(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        mw = make_summarization_middleware(summary_prompt="Summarize {messages}")
        # An unbounded verbatim re-add of the first K messages is what made a
        # wedged thread unrecoverable (SupportIncident #246).
        assert 0 < mw.initial_preserve_budget() < SUMMARIZATION_TOKEN_THRESHOLD


# ===========================================================================
# SupportIncident #246 — the summarization loop
#
# A thread on `leo-nefe` triggered summarization on every step and never got a
# turn's work done. The machinery was fine; it was handed bytes it structurally
# could not reclaim: the first 3 human messages were re-added VERBATIM with no
# byte budget, and one of them carried a 375 KB `<SELECTED_ELEMENT>` block. That
# pins the count above the trigger permanently — compaction can never win, and
# the customer has to abandon the thread.
#
# These use the REAL production constants and the REAL token counter, because
# the bug is a property of those numbers, not of the code shape.
# ===========================================================================

from app.agents.leonardo.summarization import _strip_images_then_count  # noqa: E402
from app.agents.utils.token_counter import tiktoken_token_counter  # noqa: E402

_COUNTER = _strip_images_then_count(tiktoken_token_counter)
_THRESHOLD = SUMMARIZATION_TOKEN_THRESHOLD


def _text_of_tokens(tokens: int) -> str:
    """Realistic page markup sized to roughly `tokens` tokens."""
    unit = "<div class='row'>a meeting transcript row of text</div>"
    sample = unit * 100
    per_char = _COUNTER([HumanMessage(content=sample)]) / len(sample)
    return unit * int(tokens / per_char / len(unit) + 1)


def _picked_element_message(tokens: int, msg_id: str, ask: str) -> HumanMessage:
    """The exact shape the element picker produces: intent + raw outerHTML."""
    return HumanMessage(
        content=(
            f"{ask}\n\n<SELECTED_ELEMENT>\n<section class='transcripts'>"
            f"{_text_of_tokens(tokens)}</section>\n</SELECTED_ELEMENT>"
        ),
        id=msg_id,
    )


def _prod_mw(keep_initial_human=3):
    return RailsSummarizationMiddleware(
        model=_FakeSummaryModel(),
        trigger=("tokens", _THRESHOLD),
        keep=("tokens", SUMMARIZATION_KEEP_TOKENS),
        token_counter=_COUNTER,
        trim_tokens_to_summarize=None,
        summary_prompt="Summarize:\n{messages}",
        keep_initial_human=keep_initial_human,
    )


def _wedged_by_initial_messages():
    """Repro A: three fat early messages, then a normal working conversation."""
    msgs = []
    for i in range(3):
        msgs.append(_picked_element_message(
            int(_THRESHOLD * 0.35), f"h{i}", f"fix the transcript list ({i})",
        ))
        msgs.append(AIMessage(content="on it", id=f"a{i}"))
    for i in range(20):
        msgs.append(HumanMessage(content=f"follow-up {i}", id=f"f{i}"))
        msgs.append(AIMessage(content="done " + _text_of_tokens(400), id=f"fa{i}"))
    return msgs


def _wedged_by_newest_message():
    """Cause B: the newest message alone is over the trigger.

    Stock `SummarizationMiddleware` never summarizes the current turn's message,
    so the preserved tail stays above the trigger and `before_model` re-fires on
    the very next step, forever.
    """
    msgs = []
    for i in range(30):
        msgs.append(HumanMessage(content=f"step {i}", id=f"s{i}"))
        msgs.append(AIMessage(content="ok " + _text_of_tokens(400), id=f"sa{i}"))
    msgs.append(_picked_element_message(
        int(_THRESHOLD * 1.05), "big", "Keep transcription collapsed by default",
    ))
    return msgs


def _compact(mw, convo):
    """Run one compaction and rebuild state through the real delta reducer."""
    result = mw.before_model({"messages": convo}, runtime=None)
    assert result is not None, "precondition: this conversation must summarize"
    return messages_delta_reducer(convo, [result["messages"]])


class TestSummarizationLoop:
    def test_precondition_conversations_are_over_the_trigger(self):
        assert _COUNTER(_wedged_by_initial_messages()) > _THRESHOLD
        assert _COUNTER(_wedged_by_newest_message()) > _THRESHOLD

    def test_fat_initial_messages_no_longer_pin_the_thread(self):
        """The core bug: after compaction the count must actually be under."""
        convo = _wedged_by_initial_messages()
        after = _compact(_prod_mw(), convo)
        assert _COUNTER(after) < _THRESHOLD

    def test_compaction_does_not_immediately_re_trigger(self):
        """The symptom the customer saw: summarize, think about nothing, repeat."""
        mw = _prod_mw()
        after = _compact(mw, _wedged_by_initial_messages())
        assert mw.before_model({"messages": after}, runtime=None) is None

    def test_oversized_newest_message_does_not_wedge_the_thread(self):
        mw = _prod_mw()
        after = _compact(mw, _wedged_by_newest_message())
        assert _COUNTER(after) < _THRESHOLD
        assert mw.before_model({"messages": after}, runtime=None) is None

    def test_user_intent_survives_the_truncation(self):
        """Preserving intent is the point of the feature; 375 KB of markup is not."""
        after = _compact(_prod_mw(), _wedged_by_initial_messages())
        preserved = "\n".join(
            m.content for m in after
            if isinstance(m, HumanMessage) and isinstance(m.content, str)
        )
        assert "fix the transcript list (0)" in preserved
        assert "[truncated:" in preserved

    def test_preserved_initial_messages_stay_within_budget(self):
        mw = _prod_mw()
        result = mw.before_model({"messages": _wedged_by_initial_messages()}, runtime=None)
        body = result["messages"][1:]
        summary_idx = next(
            i for i, m in enumerate(body)
            if (getattr(m, "additional_kwargs", None) or {}).get("lc_source") == "summarization"
        )
        initial = body[:summary_idx]
        assert initial, "the original ask must still be preserved"
        assert _COUNTER(initial) <= mw.initial_preserve_budget() * 1.1

    def test_small_initial_messages_are_still_preserved_verbatim(self):
        """The budget must not evict ordinary short messages."""
        convo = [HumanMessage(content="Build me a blog", id="h1")]
        filler = _text_of_tokens(4000)
        for i in range(45):
            convo.append(AIMessage(content="working " + filler, id=f"a{i}"))
            convo.append(HumanMessage(content=f"and {i}", id=f"h{i+2}"))
        after = _compact(_prod_mw(keep_initial_human=1), convo)
        assert any(
            isinstance(m, HumanMessage) and m.content == "Build me a blog"
            for m in after
        ), "a short first message must survive compaction untouched"


class TestUncompactableThreadFailsLoud:
    """4.4 — never loop silently. Log, break the loop, tell the user."""

    def test_logs_an_error_when_compaction_cannot_get_under_the_trigger(self, caplog):
        with caplog.at_level("ERROR"):
            _compact(_prod_mw(), _wedged_by_newest_message())
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, "an uncompactable thread is a guaranteed infinite loop — say so"
        text = "\n".join(r.getMessage() for r in errors)
        assert "summariz" in text.lower()

    def test_tells_the_agent_content_was_dropped_so_the_user_hears_it(self):
        after = _compact(_prod_mw(), _wedged_by_newest_message())
        summary = next(
            m for m in after
            if (getattr(m, "additional_kwargs", None) or {}).get("lc_source") == "summarization"
        )
        assert "TRUNCATED" in summary.content.upper()

    def test_files_a_friction_report(self, monkeypatch):
        import app.agents.leonardo.friction as friction

        friction.reset_friction_tracking()
        sent = []
        monkeypatch.setattr(friction, "dispatch_friction_report", sent.append)

        _compact(_prod_mw(), _wedged_by_newest_message())
        assert sent, "this is exactly the self-reported tooling failure friction is for"
        assert sent[0]["error_class"].startswith("AgentFriction.")

    def test_friction_failure_never_breaks_compaction(self, monkeypatch):
        import app.agents.leonardo.friction as friction

        friction.reset_friction_tracking()

        def _boom(_report):
            raise RuntimeError("mothership down")

        monkeypatch.setattr(friction, "dispatch_friction_report", _boom)
        after = _compact(_prod_mw(), _wedged_by_newest_message())
        assert _COUNTER(after) < _THRESHOLD

    def test_no_error_and_no_friction_on_a_healthy_compaction(self, monkeypatch, caplog):
        import app.agents.leonardo.friction as friction

        friction.reset_friction_tracking()
        sent = []
        monkeypatch.setattr(friction, "dispatch_friction_report", sent.append)

        with caplog.at_level("ERROR"):
            _compact(_prod_mw(), _wedged_by_initial_messages())
        assert not sent
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    def test_tool_call_pairs_are_never_broken_by_force_truncation(self):
        """Truncation must edit content, never drop a message out of a pair."""
        convo = _wedged_by_newest_message()
        convo.insert(0, ToolMessage(content="x" * 200, tool_call_id="tc0", id="t0"))
        convo.insert(0, AIMessage(
            content="", id="a0",
            tool_calls=[{"name": "read_file", "id": "tc0", "args": {"path": "a.rb"}}],
        ))
        after = _compact(_prod_mw(), convo)
        tool_call_ids = {
            tc["id"] for m in after for tc in (getattr(m, "tool_calls", None) or [])
        }
        for m in after:
            if isinstance(m, ToolMessage):
                assert m.tool_call_id in tool_call_ids


class TestThreadRepair:
    """`/compact` is the user's rescue lever for a thread that is ALREADY wedged.

    Threads created before this shipped still carry the oversized messages, and
    the customer's only alternative is to abandon their context and start over.
    """

    def test_oversized_messages_are_truncated_in_place(self):
        from app.agents.leonardo.summarization import truncate_oversized_messages

        convo = [
            HumanMessage(content="fix the transcript list", id="h1"),
            AIMessage(content="on it", id="a1"),
            _picked_element_message(int(_THRESHOLD * 0.7), "big", "and this one"),
        ]
        repaired, count = truncate_oversized_messages(convo, 15000, _COUNTER)

        assert count == 1
        assert _COUNTER([repaired[-1]]) <= 15000 * 1.1
        assert repaired[-1].id == "big"
        assert "and this one" in repaired[-1].content
        assert "[truncated:" in repaired[-1].content
        # Untouched messages are the SAME objects, not rebuilt copies.
        assert repaired[0] is convo[0] and repaired[1] is convo[1]

    def test_nothing_to_repair_is_a_no_op(self):
        from app.agents.leonardo.summarization import truncate_oversized_messages

        convo = [HumanMessage(content="hi", id="h1"), AIMessage(content="hello", id="a1")]
        repaired, count = truncate_oversized_messages(convo, 15000, _COUNTER)
        assert count == 0
        assert repaired == convo

    def test_tool_messages_keep_their_call_id(self):
        from app.agents.leonardo.summarization import truncate_oversized_messages

        convo = [
            AIMessage(content="", id="a1", tool_calls=[
                {"name": "read_file", "id": "tc1", "args": {"path": "a.rb"}},
            ]),
            ToolMessage(content=_text_of_tokens(int(_THRESHOLD * 0.5)),
                        tool_call_id="tc1", id="t1"),
        ]
        repaired, count = truncate_oversized_messages(convo, 15000, _COUNTER)
        assert count == 1
        assert repaired[1].tool_call_id == "tc1"
        assert _COUNTER([repaired[1]]) <= 15000 * 1.1


class TestHardCeiling:
    """Truncating text is not always enough — and "we tried" is still a dead
    thread for the customer.

    Tool-call arguments and image blocks aren't text, so a message can be
    unshrinkable. When that happens the payload handed to the model must still
    come in under the trigger, even if that means dropping messages entirely and
    sending a single summary with nothing attached to it.
    """

    def _unshrinkable_convo(self):
        """Every fat message here is fat in a way truncation cannot fix."""
        fat_args = {"content": _text_of_tokens(int(_THRESHOLD * 0.4))}
        convo = []
        for i in range(3):
            convo.append(AIMessage(content="", id=f"a{i}", tool_calls=[
                {"name": "write_file", "id": f"tc{i}", "args": dict(fat_args)},
            ]))
            convo.append(ToolMessage(content="ok", tool_call_id=f"tc{i}", id=f"t{i}"))
        convo.append(HumanMessage(content="now fix the header", id="last"))
        return convo

    def test_payload_is_under_the_trigger_even_when_nothing_can_be_truncated(self):
        mw = _prod_mw()
        convo = self._unshrinkable_convo()
        assert _COUNTER(convo) > _THRESHOLD
        after = _compact(mw, convo)
        assert _COUNTER(after) < _THRESHOLD
        assert mw.before_model({"messages": after}, runtime=None) is None

    def test_dropping_never_leaves_a_dangling_tool_call(self):
        after = _compact(_prod_mw(), self._unshrinkable_convo())
        answered = {m.tool_call_id for m in after if isinstance(m, ToolMessage)}
        for m in after:
            for tc in (getattr(m, "tool_calls", None) or []):
                assert tc["id"] in answered, "a tool_call with no answer 400s the request"

    def test_stripping_media_keeps_the_message_and_says_what_went(self):
        """An image the agent can re-request beats a thread that can't take a turn."""
        img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        msg = ToolMessage(
            content=[{"type": "text", "text": "the page"}, img],
            tool_call_id="tc1", id="t1",
        )
        stripped = _prod_mw()._strip_media(msg)

        assert not [b for b in stripped.content if b.get("type") == "image_url"]
        assert stripped.tool_call_id == "tc1" and stripped.id == "t1"
        text = " ".join(b.get("text", "") for b in stripped.content)
        assert "the page" in text and "Attachment removed" in text

    def test_no_image_survives_a_payload_that_had_to_be_forced_under(self):
        img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        convo = self._unshrinkable_convo()
        convo.insert(1, ToolMessage(
            content=[{"type": "text", "text": "screenshot"}, img],
            tool_call_id="tc0", id="shot",
        ))
        after = _compact(_prod_mw(), convo)
        assert _COUNTER(after) < _THRESHOLD
        blocks = [
            b for m in after if isinstance(getattr(m, "content", None), list)
            for b in m.content if isinstance(b, dict)
        ]
        assert not [b for b in blocks if b.get("type") == "image_url"]

    def test_worst_case_is_a_summary_with_nothing_attached(self):
        """One message, unshrinkable, bigger than the whole budget on its own."""
        mw = _prod_mw()
        convo = [
            HumanMessage(content="start", id="h0"),
            AIMessage(content="", id="a1", tool_calls=[
                {"name": "write_file", "id": "tc1",
                 "args": {"content": _text_of_tokens(int(_THRESHOLD * 1.4))}},
            ]),
            ToolMessage(content="ok", tool_call_id="tc1", id="t1"),
        ]
        after = _compact(mw, convo)
        assert _COUNTER(after) < _THRESHOLD
        assert any(
            (getattr(m, "additional_kwargs", None) or {}).get("lc_source") == "summarization"
            for m in after
        ), "the summary is the one thing that must always survive"
        assert mw.before_model({"messages": after}, runtime=None) is None


class TestCeilingWhenSummarizationDeclines:
    """`SummarizationMiddleware` returns None when it finds no safe cutoff — the
    count stays over the trigger and the oversized payload goes to the provider
    anyway. The ceiling has to apply there too."""

    def test_oversized_single_message_thread_is_still_bounded(self):
        mw = _prod_mw()
        convo = [_picked_element_message(int(_THRESHOLD * 1.3), "only", "fix this")]
        result = mw.before_model({"messages": convo}, runtime=None)
        assert result is not None, "an over-trigger payload must never be passed through"
        after = messages_delta_reducer(convo, [result["messages"]])
        assert _COUNTER(after) < _THRESHOLD

    def test_a_healthy_thread_is_left_completely_alone(self):
        mw = _prod_mw()
        convo = [HumanMessage(content="hi", id="h1"), AIMessage(content="hello", id="a1")]
        assert mw.before_model({"messages": convo}, runtime=None) is None


class TestLoopBreakerCatchesPermanentBallast:
    """The 2026-08-13 incident: compaction reclaiming nothing, silently.

    A 74k-token `grep_files` result (one match inside a minified vendor bundle)
    is larger than the 30k keep-tail, and `SummarizationMiddleware` never splits
    a tool-call group — so it was re-preserved by every compaction for the rest
    of the thread's life. Each pass shed only the ordinary history around it,
    bought two or three steps of headroom, and re-fired. 22 times, 18 minutes,
    no output.

    Note what the totals looked like while that happened: ~90-101k against a
    150k trigger. Under the trigger AND under the 0.8 post-compaction target, so
    neither a trigger check nor a target check would have said a word. The thing
    that makes a thread doomed is not its total — it is carrying a message no
    future compaction can remove.
    """

    TARGET = int(_THRESHOLD * POST_COMPACTION_TARGET_RATIO)

    def _incident_shape(self, ballast_tokens=None):
        """A compacted thread at the incident's real numbers: ~101k, doomed.

        Summary + an uncompactable 74k tool result + ordinary tail.
        """
        if ballast_tokens is None:
            ballast_tokens = int(_THRESHOLD * 0.49)
        summary = HumanMessage(
            content="SUMMARY OF THE CONVERSATION SO FAR",
            id="sum",
            additional_kwargs={"lc_source": "summarization"},
        )
        caller = AIMessage(content="", id="grepper", tool_calls=[
            {"name": "grep_files", "id": "tcg", "args": {"pattern": r"confirm\("}},
        ])
        ballast = ToolMessage(
            content=_text_of_tokens(ballast_tokens), tool_call_id="tcg", id="tg",
        )
        tail = AIMessage(content="ok " + _text_of_tokens(20000), id="tail")
        return [summary, caller, ballast, tail]

    def test_precondition_the_incident_looked_healthy_by_every_total(self):
        messages = self._incident_shape()
        total = _COUNTER(messages)
        assert total < _THRESHOLD, "under the trigger — the old guard returned here"
        assert total < self.TARGET, (
            "and under the target too, so comparing against the target instead "
            "of the trigger would ALSO have missed this incident"
        )
        ballast = next(m for m in messages if getattr(m, "id", None) == "tg")
        assert _COUNTER([ballast]) > SUMMARIZATION_KEEP_TOKENS, (
            "this is what actually makes it fatal: bigger than the preserved "
            "tail means no future compaction can ever remove it"
        )

    def test_an_uncompactable_message_is_cut_down_to_size(self):
        mw = _prod_mw()
        out = mw._break_loop_if_still_oversized(self._incident_shape(), None)
        for m in out:
            assert _COUNTER([m]) <= SUMMARIZATION_KEEP_TOKENS, (
                "nothing may remain that a future compaction cannot shed"
            )

    def test_it_fails_loud(self, caplog):
        with caplog.at_level("ERROR"):
            _prod_mw()._break_loop_if_still_oversized(self._incident_shape(), None)
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, (
            "a compaction that leaves the thread doomed logged absolutely "
            "nothing — which is why this looked like a hang, not a known bug"
        )

    def test_it_files_a_friction_report(self, monkeypatch):
        import app.agents.leonardo.friction as friction

        friction.reset_friction_tracking()
        sent = []
        monkeypatch.setattr(friction, "dispatch_friction_report", sent.append)

        _prod_mw()._break_loop_if_still_oversized(self._incident_shape(), None)
        assert sent, "we only see this fleet-wide if it reaches /admin/agent_friction"

    def test_the_tool_call_pair_survives_being_cut_down(self):
        """Truncating ballast must never orphan a tool_call — that 400s."""
        out = _prod_mw()._break_loop_if_still_oversized(self._incident_shape(), None)
        answered = {m.tool_call_id for m in out if isinstance(m, ToolMessage)}
        for m in out:
            for tc in (getattr(m, "tool_calls", None) or []):
                assert tc["id"] in answered

    def test_a_healthy_compaction_is_left_completely_alone(self, monkeypatch, caplog):
        """No noise and no truncation when nothing is uncompactable."""
        import app.agents.leonardo.friction as friction

        friction.reset_friction_tracking()
        sent = []
        monkeypatch.setattr(friction, "dispatch_friction_report", sent.append)

        mw = _prod_mw()
        messages = self._incident_shape(ballast_tokens=int(SUMMARIZATION_KEEP_TOKENS * 0.5))
        assert _COUNTER(messages) < self.TARGET  # precondition
        with caplog.at_level("ERROR"):
            out = mw._break_loop_if_still_oversized(messages, None)

        assert out is messages
        assert not sent
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    def test_a_compaction_over_the_target_is_still_forced_under_it(self):
        """The other half of the guard: the target, not just the trigger.

        A pass landing at 0.9x the trigger has only a step or two of headroom
        left, so it is a loop even if nothing single message is oversized.
        """
        mw = _prod_mw()
        messages = [
            HumanMessage(
                content="SUMMARY", id="sum",
                additional_kwargs={"lc_source": "summarization"},
            ),
        ] + [
            AIMessage(content="step " + _text_of_tokens(20000), id=f"a{i}")
            for i in range(7)
        ]
        total = _COUNTER(messages)
        assert self.TARGET < total < _THRESHOLD, "precondition: the missed window"
        assert _COUNTER(mw._break_loop_if_still_oversized(messages, None)) <= self.TARGET

    def test_the_thread_does_not_grow_pass_over_pass(self):
        """The ground truth from the incident, end to end.

        The "compacted" payload written to `checkpoint_writes` GREW on every
        pass — 342 KB -> 457 KB across five — which is what reclaiming nothing
        looks like from the outside. Run several turns of real work through the
        middleware and every compaction must land under the target, with no
        upward drift.
        """
        mw = _prod_mw()
        convo = _wedged_by_newest_message()
        compacted_sizes = []
        for _ in range(6):
            result = mw.before_model({"messages": convo}, runtime=None)
            if result is not None:
                convo = messages_delta_reducer(convo, [result["messages"]])
                compacted_sizes.append(_COUNTER(convo))
            # a turn's worth of ordinary work on top
            convo.append(AIMessage(content="more " + _text_of_tokens(30000), id=None))
            convo.append(HumanMessage(content="continue", id=None))

        assert len(compacted_sizes) >= 2, "precondition: several compactions ran"
        assert max(compacted_sizes) <= self.TARGET, (
            f"a compaction landed over the target: {compacted_sizes}"
        )
        # Every pass must settle at roughly the keep-tail plus a summary. The
        # incident's signature was the opposite: each "compacted" payload bigger
        # than the last, because the ballast was all that ever survived.
        assert max(compacted_sizes) <= SUMMARIZATION_KEEP_TOKENS * 2, (
            f"compaction is not reclaiming the history it should: {compacted_sizes}"
        )


class TestWedgedThreadIsVisibleToTheUser:
    """Today the customer's only signal is that nothing happens.

    A turn that compacts repeatedly is a thread that has outgrown its context.
    The middleware records that on the turn so the request handler can say so in
    chat instead of spinning silently.
    """

    @pytest.fixture
    def turn(self):
        from app.lib.turn_metrics import _current_turn, start_turn

        token = _current_turn.set(None)
        yield start_turn(thread_id="t1", agent_mode="rails_agent")
        _current_turn.reset(token)

    def test_a_compaction_is_recorded_on_the_current_turn(self, turn):
        _compact(_prod_mw(), _wedged_by_initial_messages())
        assert turn.compaction_count == 1

    def test_repeated_compactions_in_one_turn_mark_the_thread_wedged(self, turn):
        from app.lib.turn_metrics import COMPACTIONS_BEFORE_USER_WARNING

        assert not turn.thread_is_wedged()
        for _ in range(COMPACTIONS_BEFORE_USER_WARNING):
            turn.record_compaction()
        assert turn.thread_is_wedged()

    def test_compaction_count_rides_along_in_the_metrics_snapshot(self, turn):
        turn.record_compaction()
        assert turn.snapshot(total_ms=10.0)["compactions"] == 1

    def test_recording_without_a_turn_is_harmless(self):
        """Headless runs and tests have no recorder installed."""
        _compact(_prod_mw(), _wedged_by_initial_messages())
