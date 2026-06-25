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
