"""Tests for make_summarization_model() — DeepSeek fallback when no Gemini key.

Regression guard for the fleet-wide graph-compile failure that occurred when
GOOGLE_API_KEY / GEMINI_API_KEY were absent (SupportIncident #93).
"""
import pytest

from app.agents.leonardo.llm_factory import ChatDeepSeekWithReasoning, make_summarization_model
from langchain_google_genai import ChatGoogleGenerativeAI


@pytest.fixture(autouse=True)
def _clear_google_keys(monkeypatch):
    """Strip Google key env vars by default; individual tests opt back in.
    Always set a dummy DEEPSEEK_API_KEY so ChatDeepSeekWithReasoning can
    instantiate in CI (where no real key is present). In production the real
    key is always configured — this mirrors that assumption in tests."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ci-placeholder")


class TestMakeSummarizationModelNoKey:
    def test_returns_deepseek_model_when_no_google_key(self):
        model, token_counter, trim = make_summarization_model()
        assert isinstance(model, ChatDeepSeekWithReasoning)

    def test_no_raise_when_no_google_key(self):
        # Must not raise — this is the fleet-wide compile-time guard
        make_summarization_model()

    def test_token_counter_is_callable(self):
        _, token_counter, _ = make_summarization_model()
        assert callable(token_counter)

    def test_token_counter_does_not_call_gemini_api(self, monkeypatch):
        """tiktoken-only path must work without any Google API call."""
        from app.agents.utils import token_counter as tc_module
        called = []
        monkeypatch.setattr(tc_module, "_get_genai_client", lambda: called.append(1) or (_ for _ in ()).throw(AssertionError("Gemini API called on DeepSeek path")))
        from langchain_core.messages import HumanMessage
        _, token_counter, _ = make_summarization_model()
        count = token_counter([HumanMessage(content="hello world")])
        assert count > 0
        assert called == [], "tiktoken_token_counter must not call Gemini API"

    def test_trim_tokens_is_set_for_deepseek(self):
        _, _, trim = make_summarization_model()
        assert trim is not None
        assert isinstance(trim, int)
        assert trim > 0


class TestMakeSummarizationModelWithGeminiKey:
    def test_returns_gemini_model_when_google_api_key_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-key-for-test")
        model, token_counter, trim = make_summarization_model()
        assert isinstance(model, ChatGoogleGenerativeAI)

    def test_returns_gemini_model_when_gemini_api_key_set(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
        model, token_counter, trim = make_summarization_model()
        assert isinstance(model, ChatGoogleGenerativeAI)

    def test_trim_is_none_for_gemini_path(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "fake-key-for-test")
        _, _, trim = make_summarization_model()
        assert trim is None


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
