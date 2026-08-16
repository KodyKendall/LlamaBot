"""Provider SDKs must not silently multiply application-owned retries."""

import pytest
import logging

from app.agents.leonardo import llm_factory
from app.agents.leonardo import model_policy


@pytest.mark.parametrize(
    ("model_name", "constructor_name", "retry_key"),
    [
        ("deepseek-v4-flash", "ChatDeepSeekWithReasoning", "max_retries"),
        ("deepseek-v4-flash-gmi", "ChatDeepSeekWithReasoning", "max_retries"),
        ("deepseek-v4-flash-fireworks", "ChatDeepSeekWithReasoning", "max_retries"),
        ("gpt-5-mini", "ChatOpenAI", "max_retries"),
        ("claude-4.5-haiku", "ChatAnthropic", "max_retries"),
        ("gemini-3-flash", "ChatGoogleGenerativeAI", "retries"),
        ("qwen3.7-plus", "ChatQwen", "max_retries"),
        ("qwen3-8b-runpod", "ChatOpenAI", "max_retries"),
        ("muse-glimmer-30b-runpod", "ChatOpenAI", "max_retries"),
        ("nemotron-lightning-30b-runpod", "ChatOpenAI", "max_retries"),
        ("nemotron-lightning-30b-fireworks", "ChatDeepSeekWithReasoning", "max_retries"),
    ],
)
def test_get_llm_disables_hidden_provider_retries(
    monkeypatch, model_name, constructor_name, retry_key
):
    captured = {}

    def fake_constructor(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(model_policy, "is_model_enabled", lambda _: True)
    monkeypatch.setattr(llm_factory, constructor_name, fake_constructor)

    llm_factory.get_llm(model_name)

    assert captured[retry_key] == 0


def test_gmi_deepseek_routes_to_gmi_endpoint_and_key(monkeypatch):
    """The GMI entry must stay a distinct provider path from DeepSeek direct.

    Guards the whole point of the separate model name: it has to reach GMI's
    endpoint with GMI's key and GMI's namespaced model id. A regression here
    would silently bill/route to api.deepseek.com under a GMI-labelled option.
    """
    captured = {}

    def fake_constructor(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(model_policy, "is_model_enabled", lambda _: True)
    monkeypatch.setattr(llm_factory, "ChatDeepSeekWithReasoning", fake_constructor)
    monkeypatch.setenv("GMI_DEEPSEEK_API_KEY", "gmi-test-key")
    monkeypatch.delenv("GMI_BASE_URL", raising=False)
    monkeypatch.delenv("GMI_DEEPSEEK_MODEL", raising=False)

    llm_factory.get_llm("deepseek-v4-flash-gmi")

    assert captured["api_base"] == "https://api.gmi-serving.com/v1"
    assert captured["api_key"] == "gmi-test-key"
    assert captured["model"] == "deepseek-ai/DeepSeek-V4-Flash"


def test_fireworks_deepseek_routes_to_fireworks_endpoint_and_key(monkeypatch):
    """The Fireworks entry must stay a distinct provider path.

    Fireworks was chosen for data-retention reasons (US-hosted) — a regression
    that silently routed this to DeepSeek direct or GMI would break the very
    guarantee the entry exists to provide, without any visible symptom.
    """
    captured = {}

    def fake_constructor(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(model_policy, "is_model_enabled", lambda _: True)
    monkeypatch.setattr(llm_factory, "ChatDeepSeekWithReasoning", fake_constructor)
    monkeypatch.setenv("FIREWORKS_DEEPSEEK_API_KEY", "fw-test-key")
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)
    monkeypatch.delenv("FIREWORKS_DEEPSEEK_MODEL", raising=False)

    llm_factory.get_llm("deepseek-v4-flash-fireworks")

    assert captured["api_base"] == "https://api.fireworks.ai/inference/v1"
    assert captured["api_key"] == "fw-test-key"
    assert captured["model"] == "accounts/fireworks/models/deepseek-v4-flash"


def test_deepseek_direct_does_not_point_at_third_party(monkeypatch):
    """The plain DeepSeek entry must keep using DeepSeek's own API."""
    captured = {}

    def fake_constructor(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(model_policy, "is_model_enabled", lambda _: True)
    monkeypatch.setattr(llm_factory, "ChatDeepSeekWithReasoning", fake_constructor)
    monkeypatch.setenv("GMI_BASE_URL", "https://api.gmi-serving.com/v1")
    monkeypatch.setenv("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")

    llm_factory.get_llm("deepseek-v4-flash")

    assert "api_base" not in captured
    assert captured["model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_application_retry_is_warning_logged(monkeypatch, caplog):
    from app.agents.leonardo.rails_agent import middleware

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

    class Request:
        state = {"llm_model": "deepseek-v4-flash"}

        def override(self, **kwargs):
            return self

    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("provider stalled")
        return "ok"

    monkeypatch.setattr(middleware, "get_llm", lambda _: Model())
    monkeypatch.setattr(middleware.asyncio, "sleep", lambda _: _completed_sleep())
    with caplog.at_level(logging.WARNING):
        result = await middleware.DynamicModelMiddleware().awrap_model_call(
            Request(), handler
        )

    assert result == "ok"
    assert "Transient model error" in caplog.text
    assert "retrying" in caplog.text


async def _completed_sleep():
    return None
