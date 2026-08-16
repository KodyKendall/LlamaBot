"""Tests for make_summarization_model() — provider fallback chain.

Priority: DeepSeek -> Gemini 3 Flash -> OpenAI -> Anthropic (first key present
wins). Also a fleet-wide graph-compile guard: compilation must never raise just
because a key is missing (SupportIncident #93).
"""
import pytest

from app.agents.leonardo.llm_factory import ChatDeepSeekWithReasoning, make_summarization_model
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic


@pytest.fixture(autouse=True)
def _clear_all_provider_keys(monkeypatch):
    """Strip every provider key by default; each test opts the ones it needs
    back in. This lets us assert the exact priority order of the fallback chain."""
    for var in ("DEEPSEEK_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestFallbackPriority:
    def test_deepseek_wins_when_all_keys_present(self, monkeypatch):
        for var in ("DEEPSEEK_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.setenv(var, "k")
        model, _, _ = make_summarization_model()
        assert isinstance(model, ChatDeepSeekWithReasoning)

    def test_gemini_when_no_deepseek(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "k")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        model, _, _ = make_summarization_model()
        assert isinstance(model, ChatGoogleGenerativeAI)

    def test_gemini_api_key_alias(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        model, _, _ = make_summarization_model()
        assert isinstance(model, ChatGoogleGenerativeAI)

    def test_openai_when_no_deepseek_or_gemini(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        model, _, _ = make_summarization_model()
        assert isinstance(model, ChatOpenAI)

    def test_anthropic_when_only_anthropic(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        model, _, _ = make_summarization_model()
        assert isinstance(model, ChatAnthropic)


class TestCompileGuard:
    def test_no_raise_when_no_key_at_all(self):
        # Fleet-wide compile-time guard: must return a model, never raise.
        model, token_counter, trim = make_summarization_model()
        assert isinstance(model, ChatDeepSeekWithReasoning)
        assert callable(token_counter)

    def test_deepseek_path_trim_is_set(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        _, _, trim = make_summarization_model()
        assert isinstance(trim, int) and trim > 0

    def test_gemini_path_trim_is_none(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "k")
        _, _, trim = make_summarization_model()
        assert trim is None

    def test_token_counter_does_not_call_gemini_api_on_deepseek_path(self, monkeypatch):
        """tiktoken-only path must work without any Google API call."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        from app.agents.utils import token_counter as tc_module
        called = []
        monkeypatch.setattr(
            tc_module, "_get_genai_client",
            lambda: called.append(1) or (_ for _ in ()).throw(
                AssertionError("Gemini API called on DeepSeek path")),
        )
        from langchain_core.messages import HumanMessage
        _, token_counter, _ = make_summarization_model()
        assert token_counter([HumanMessage(content="hello world")]) > 0
        assert called == []


class TestAgentGraphCompileNoGeminiKey:
    """Each of the four agents must compile their graph without a Google key."""

    def test_rails_user_mode_agent_compiles(self):
        from app.agents.leonardo.rails_user_mode_agent.nodes import build_workflow
        graph = build_workflow()
        assert graph is not None

    def test_rails_ticket_mode_agent_compiles(self):
        from app.agents.leonardo.rails_ticket_mode_agent.nodes import build_workflow
        graph = build_workflow()
        assert graph is not None

    def test_rails_plan_mode_agent_compiles(self):
        from app.agents.leonardo.rails_plan_mode_agent.nodes import build_workflow
        graph = build_workflow()
        assert graph is not None

    def test_rails_user_feedback_agent_compiles(self):
        from app.agents.leonardo.rails_user_feedback_agent.nodes import build_workflow
        graph = build_workflow()
        assert graph is not None


# ---------------------------------------------------------------------------
# A summary must be prose, not the model's tool-call syntax
# ---------------------------------------------------------------------------

class TestCorruptSummaryIsRejected:
    """The summarization model can emit a tool call instead of a summary.

    In the 2026-08-13 incident the stored summary was raw DeepSeek markup:
    `<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="read_file">...`. It carries no
    information, it is re-preserved by every future compaction, and it reads to
    the model as an instruction to go call a tool — so the agent re-derived
    context it already had, generated more messages, and fed the loop that
    compaction was supposed to end.
    """

    DEEPSEEK_MARKUP = (
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="read_file">'
        '<｜｜DSML｜｜parameter name="path">app/models/user.rb'
    )

    @pytest.mark.parametrize("markup", [
        DEEPSEEK_MARKUP,
        '<tool_call>{"name": "read_file"}</tool_call>',
        '<function_call>read_file</function_call>',
        '<invoke name="grep_files">',
        "<|tool_calls_begin|>",
    ])
    def test_tool_call_markup_is_detected(self, markup):
        from app.agents.leonardo.summarization import looks_like_tool_call_markup

        assert looks_like_tool_call_markup(markup)

    @pytest.mark.parametrize("prose", [
        "The user asked for a blog. We scaffolded Post and Comment models.",
        "Summary: fixed the failing test in spec/models/user_spec.rb.",
        # Prose that merely mentions tools must NOT trip the check.
        "I called read_file on app/models/user.rb and grep_files for 'confirm('.",
        "We discussed the function call convention for the API.",
    ])
    def test_ordinary_summaries_are_left_alone(self, prose):
        from app.agents.leonardo.summarization import (
            looks_like_tool_call_markup,
            validate_summary_text,
        )

        assert not looks_like_tool_call_markup(prose)
        assert validate_summary_text(prose) is prose

    def test_a_corrupt_summary_is_replaced_with_something_honest(self):
        from app.agents.leonardo.summarization import validate_summary_text

        out = validate_summary_text(self.DEEPSEEK_MARKUP)
        assert out != self.DEEPSEEK_MARKUP
        assert "DSML" not in out
        # It must tell the agent the history is gone rather than let it invent one.
        assert "not recoverable" in out.lower()

    def test_it_fails_loud(self, caplog):
        from app.agents.leonardo.summarization import validate_summary_text

        with caplog.at_level("ERROR"):
            validate_summary_text(self.DEEPSEEK_MARKUP)
        assert [r for r in caplog.records if r.levelname == "ERROR"], (
            "a corrupt summary is a provider bug we need to see fleet-wide"
        )

    def test_the_middleware_never_stores_markup_as_the_summary(self):
        """End to end, through the real compaction path."""
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from app.agents.leonardo.summarization import RailsSummarizationMiddleware

        markup = self.DEEPSEEK_MARKUP

        class _CorruptSummaryModel(BaseChatModel):
            @property
            def _llm_type(self):
                return "corrupt-summary"

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content=markup))]
                )

        mw = RailsSummarizationMiddleware(
            model=_CorruptSummaryModel(),
            trigger=("tokens", 5000),
            keep=("tokens", 2000),
            token_counter=lambda msgs: 1000 * len(list(msgs)),
            trim_tokens_to_summarize=None,
            summary_prompt="Summarize:\n{messages}",
            keep_initial_human=1,
        )
        convo = [
            HumanMessage(content="build a blog", id="h1"),
            *[AIMessage(content=f"step {i}", id=f"a{i}") for i in range(8)],
        ]
        result = mw.before_model({"messages": convo}, runtime=None)
        assert result is not None, "precondition: this conversation must summarize"

        summary = next(
            m for m in result["messages"]
            if (getattr(m, "additional_kwargs", None) or {}).get("lc_source") == "summarization"
        )
        assert "DSML" not in summary.content
