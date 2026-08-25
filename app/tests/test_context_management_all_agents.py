"""Backstop tests for context management across ALL Leonardo agents.

2026-08-23 fleet telemetry: `rails_beginner_agent` was the ONLY Rails mode whose
`nodes.py` never wired `make_summarization_middleware`. It is a raw `StateGraph`
node that calls `llm_with_tools.invoke(messages)` directly, so no
`AgentMiddleware` ever runs and nothing trims or summarizes. Measured
consequences over 14 days: p90 input tokens 655k (every other mode sits under
210k), 24.1% of its turns over 400k, 32 of 33 all-time
`maximum context length is 1048576 tokens` crashes, and 63% of fleet input token
spend from 44% of turns.

These tests assert two things:

1. STRUCTURAL backstop (the real guarantee): every Leonardo agent `nodes.py`
   that builds a customer-facing workflow is wired for context management —
   either through `build_leonardo_agent` (which carries the summarization
   middleware the mode passes it) or through the shared
   `compact_messages_if_needed()` helper for raw `StateGraph` nodes. The
   property is asserted over an enumerated glob, never a hard-coded pass list,
   so the *next* mode we add cannot repeat this.
2. BEHAVIOUR: the compaction the raw nodes call actually brings an oversized
   history back under the threshold, and the node persists that compaction
   instead of paying for it again on every model call.

Note on the fix that was chosen: the ticket offered (a) rebuild beginner mode on
`create_agent`, or (b) call the same summarization code path from the raw node.
(b) shipped, because its objection to (b) — "a second implementation of the same
rule" — does not apply to `compact_messages_if_needed()`: that is a thin adapter
over the very same `RailsSummarizationMiddleware` instance the create_agent modes
run, and it fixes all THREE raw-node modes rather than only beginner. Rebuilding
the mode 44% of customer turns live in, one release before shipping, bought
nothing extra for THIS bug.
"""

import re
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.leonardo.summarization import RailsSummarizationMiddleware
from app.agents.utils.token_counter import SUMMARIZATION_TOKEN_THRESHOLD

LEONARDO_DIR = Path(__file__).resolve().parents[1] / "agents" / "leonardo"

# Every Leonardo graph registered in app/langgraph.json. Guards the glob below
# against silently scanning nothing.
LEONARDO_GRAPH_KEYS = [
    "rails_agent",
    "rails_ai_builder_agent",
    "rails_testing_agent",
    "rails_ticket_mode_agent",
    "rails_ticket_plan_mode_agent",
    "rails_user_mode_agent",
    "rails_beginner_agent",
    "rails_plan_mode_agent",
    "rails_engineer_plan_mode_agent",
    "pyxl_agent",
]


def _agent_nodes_files():
    return sorted(LEONARDO_DIR.glob("*/nodes.py"))


# --------------------------------------------------------------------------
# 1. Structural backstop
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "nodes_file", _agent_nodes_files(), ids=lambda p: p.parent.name
)
def test_every_leonardo_agent_has_context_management(nodes_file):
    """A mode with no ceiling on its history reaches the provider's wall."""
    text = nodes_file.read_text()
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())

    uses_factory = "build_leonardo_agent(" in code
    uses_raw_create_agent = bool(re.search(r"\bcreate_agent\(", code))
    uses_stategraph = "StateGraph(" in code

    if not (uses_factory or uses_raw_create_agent or uses_stategraph):
        pytest.skip(f"{nodes_file.parent.name} does not construct an agent")

    if uses_raw_create_agent and not uses_factory:
        pytest.fail(
            f"{nodes_file.parent.name} calls create_agent() directly — use "
            f"build_leonardo_agent() so the cross-cutting middleware is wired."
        )

    if uses_factory:
        assert "make_summarization_middleware" in code, (
            f"{nodes_file.parent.name} builds an agent through the factory but "
            f"passes no summarization middleware, so its history has no ceiling."
        )
        return

    # Raw StateGraph path: must compact before handing messages to the model.
    assert "compact_messages_if_needed" in code, (
        f"{nodes_file.parent.name} is a raw StateGraph agent that invokes the "
        f"model directly but never calls compact_messages_if_needed() — nothing "
        f"trims or summarizes it, so it climbs to the provider's context wall."
    )


def test_scan_covers_all_registered_leonardo_graphs():
    present = {p.parent.name for p in _agent_nodes_files()}
    missing = [k for k in LEONARDO_GRAPH_KEYS if k not in present]
    assert not missing, f"registered Leonardo graphs missing a nodes.py: {missing}"


# --------------------------------------------------------------------------
# 2. Behaviour — beginner mode actually compacts
# --------------------------------------------------------------------------

class _FakeSummaryModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "fake-summary"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="SUMMARY"))]
        )


def _tokens_of(n_tokens: int) -> str:
    # The production counter is roughly 4 chars/token for plain ASCII.
    return "word " * (n_tokens // 2)


def _oversized_history(total_tokens=400_000, per_message=20_000):
    msgs = [HumanMessage(content="Build me a booking site", id="h1")]
    for i in range(total_tokens // per_message):
        msgs.append(AIMessage(content=_tokens_of(per_message), id=f"a{i}"))
    return msgs


@pytest.fixture
def fake_compactor(monkeypatch):
    """The real middleware, with only the summarizing LLM faked out."""
    import app.agents.leonardo.summarization as summ

    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    summ._COMPACTOR_CACHE.clear()
    real = summ.make_summarization_middleware

    built = []

    def _build(**kwargs):
        mw = real(**kwargs)
        mw.model = _FakeSummaryModel()
        built.append(mw)
        return mw

    monkeypatch.setattr(summ, "make_summarization_middleware", _build)
    yield built
    summ._COMPACTOR_CACHE.clear()


class TestCompactMessagesIfNeeded:
    def test_it_is_a_no_op_under_the_threshold(self, fake_compactor):
        from app.agents.leonardo.summarization import compact_messages_if_needed

        history = [HumanMessage(content="hi"), AIMessage(content="hello")]
        out, ops = compact_messages_if_needed(history, summary_prompt="S {messages}")

        assert out == history
        assert ops == [], "a short thread must never write a compaction to state"

    def test_it_brings_an_oversized_history_under_the_threshold(self, fake_compactor):
        from app.agents.leonardo.summarization import compact_messages_if_needed

        history = _oversized_history()
        out, ops = compact_messages_if_needed(history, summary_prompt="S {messages}")

        counter = fake_compactor[0]
        assert counter._count(history) > SUMMARIZATION_TOKEN_THRESHOLD
        after = counter._count(out)
        assert after < SUMMARIZATION_TOKEN_THRESHOLD, (
            f"history still {after} tokens after compaction "
            f"(threshold {SUMMARIZATION_TOKEN_THRESHOLD}) — the turn would reach "
            f"the provider oversized"
        )

    def test_it_returns_ops_that_persist_the_compaction(self, fake_compactor):
        """Without persisting, the node re-summarizes on every model call."""
        from langchain_core.messages import RemoveMessage
        from langgraph.graph.message import REMOVE_ALL_MESSAGES
        from app.agents.leonardo.summarization import compact_messages_if_needed

        _, ops = compact_messages_if_needed(
            _oversized_history(), summary_prompt="S {messages}"
        )
        assert ops, "compaction produced no state write"
        assert isinstance(ops[0], RemoveMessage)
        assert ops[0].id == REMOVE_ALL_MESSAGES
        assert [m for m in ops if not isinstance(m, RemoveMessage)]

    def test_a_broken_compaction_never_kills_the_turn(self, monkeypatch):
        import app.agents.leonardo.summarization as summ

        summ._COMPACTOR_CACHE.clear()
        monkeypatch.setattr(
            summ, "make_summarization_middleware",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("no provider key")),
        )
        history = _oversized_history()
        out, ops = summ.compact_messages_if_needed(history, summary_prompt="S")
        assert out == history and ops == []
        summ._COMPACTOR_CACHE.clear()


class TestBeginnerNodeCompacts:
    """The mode 44% of customer turns live in, end to end through the node."""

    def _run(self, monkeypatch, history):
        import app.agents.leonardo.rails_beginner_agent.nodes as nb

        captured = {}

        class _Bound:
            def invoke(self, messages, **kw):
                captured["messages"] = messages
                return AIMessage(content="ok")

        class _Model:
            def bind_tools(self, *a, **k):
                return _Bound()

            def invoke(self, messages, **kw):
                return _Bound().invoke(messages)

        monkeypatch.setattr(nb, "get_llm", lambda name: _Model())
        monkeypatch.setattr(nb, "get_sys_msg", lambda: {"role": "system", "content": "sys"})
        monkeypatch.setattr(nb, "normalize_messages_for_provider", lambda m: m)

        out = nb.leonardo_beginner({"messages": history})
        return captured["messages"], out

    def test_the_model_never_sees_an_oversized_history(self, monkeypatch, fake_compactor):
        history = _oversized_history()
        sent, _ = self._run(monkeypatch, history)

        counter = fake_compactor[0]
        assert counter._count(history) > SUMMARIZATION_TOKEN_THRESHOLD
        # The system message rides along; count only the conversation.
        conversation = [m for m in sent if not isinstance(m, dict)]
        after = counter._count(conversation)
        assert after < SUMMARIZATION_TOKEN_THRESHOLD, (
            f"beginner mode handed the provider {after} tokens"
        )

    def test_the_compaction_is_written_back_to_state(self, monkeypatch, fake_compactor):
        from langchain_core.messages import RemoveMessage

        _, out = self._run(monkeypatch, _oversized_history())
        assert any(isinstance(m, RemoveMessage) for m in out["messages"]), (
            "the compaction was not persisted, so the next model call pays for "
            "the whole summarization again"
        )
        assert out["messages"][-1].content == "ok"

    def test_a_short_thread_is_left_completely_alone(self, monkeypatch, fake_compactor):
        history = [HumanMessage(content="build me a page")]
        _, out = self._run(monkeypatch, history)
        assert len(out["messages"]) == 1
        assert out["messages"][0].content == "ok"
