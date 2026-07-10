"""Provider SDKs must not silently multiply application-owned retries."""

import pytest
import logging

from app.agents.leonardo import llm_factory
from app.agents.leonardo import model_policy


@pytest.mark.parametrize(
    ("model_name", "constructor_name", "retry_key"),
    [
        ("deepseek-v4-flash", "ChatDeepSeekWithReasoning", "max_retries"),
        ("gpt-5-mini", "ChatOpenAI", "max_retries"),
        ("claude-4.5-haiku", "ChatAnthropic", "max_retries"),
        ("gemini-3-flash", "ChatGoogleGenerativeAI", "retries"),
        ("qwen3.7-plus", "ChatQwen", "max_retries"),
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
