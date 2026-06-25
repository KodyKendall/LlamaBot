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
