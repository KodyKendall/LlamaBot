"""
Model-agnostic transient-error classification for the resilience ladder.

The point of these tests is a single correctness contract that keeps the retry
rung from making things worse:

  * TRANSIENT infrastructure failures (rate limits, timeouts, connection drops,
    5xx) SHOULD be retried — this is what "model endpoints throwing errors"
    looks like, and a retry genuinely helps.

  * DETERMINISTIC configuration/request errors SHOULD NOT be retried. The two
    production bugs that motivated this work are the canonical examples:
      - cache_control -> TypeError (a bad kwarg for the provider)
      - image_url on a Responses-API model -> 400 invalid_request
    Retrying either just fails 3x identically and delays the fallback/floor
    that could actually recover. So the classifier must call them non-transient.

The classifier is duck-typed on HTTP status + a provider-agnostic type list, so
it works the same for DeepSeek, OpenAI, Anthropic, Gemini and Qwen without
hard-coding any one provider.
"""
import pytest

from app.agents.leonardo.resilience import (
    transient_exception_types,
    is_transient_error,
)


# --- the type tuple used to configure Runnable.with_retry ---------------------

def test_transient_types_include_builtin_infra_errors():
    types = transient_exception_types()
    assert TimeoutError in types
    assert ConnectionError in types


def test_transient_types_include_provider_rate_limit_and_5xx():
    """Provider SDK transient classes must be present so retry is model-agnostic."""
    import openai
    types = transient_exception_types()
    assert openai.RateLimitError in types          # 429
    assert openai.APITimeoutError in types
    assert openai.APIConnectionError in types
    assert openai.InternalServerError in types     # 5xx


def test_transient_types_preserve_existing_google_behavior():
    from google.api_core.exceptions import ResourceExhausted
    assert ResourceExhausted in transient_exception_types()


def test_transient_types_EXCLUDE_deterministic_errors():
    """The whole point: deterministic bugs must never be retried."""
    import openai
    types = transient_exception_types()
    # cache_control bug surfaces as a plain TypeError
    assert TypeError not in types
    assert ValueError not in types
    # image_url bug surfaces as a 400 BadRequest — must NOT be retryable
    assert openai.BadRequestError not in types
    # the broad base class would sweep in 400s/422s, so it must be excluded
    assert openai.APIError not in types


# --- the runtime predicate (used for logging / floor decisions) ---------------

def test_is_transient_true_for_infra_exceptions():
    assert is_transient_error(TimeoutError("read timed out")) is True
    assert is_transient_error(ConnectionError("reset")) is True


def test_is_transient_false_for_the_two_production_bugs():
    # cache_control
    assert is_transient_error(
        TypeError("Completions.create() got an unexpected keyword argument 'cache_control'")
    ) is False
    # image_url 400 (duck-typed via status_code, no SDK object needed)
    err_400 = _FakeStatusError(400, "unknown variant `image_url`, expected `text`")
    assert is_transient_error(err_400) is False


@pytest.mark.parametrize("code,expected", [
    (429, True), (500, True), (502, True), (503, True), (504, True), (408, True),
    (400, False), (401, False), (403, False), (404, False), (422, False),
])
def test_is_transient_duck_types_http_status(code, expected):
    assert is_transient_error(_FakeStatusError(code, "x")) is expected


def test_is_transient_false_for_unknown_plain_exception():
    """Unknown errors are NOT retried — let them fall to the fallback/floor rung."""
    assert is_transient_error(Exception("something weird")) is False


class _FakeStatusError(Exception):
    """Minimal stand-in for a provider HTTP error exposing .status_code."""
    def __init__(self, status_code, message=""):
        super().__init__(message)
        self.status_code = status_code


# --- wiring into DynamicModelMiddleware --------------------------------------

class _ToolBindableModel:
    """Stand-in chat model: the thing the framework calls .bind_tools() on."""
    def bind_tools(self, *a, **k):
        return self


class _Req:
    def __init__(self, model_name="deepseek-v4-flash"):
        self.state = {"llm_model": model_name}
        self.model = None

    def override(self, **kw):
        self.model = kw.get("model", self.model)
        return self


def test_wrap_model_call_preserves_bind_tools_sync(monkeypatch):
    """Regression: the model handed downstream MUST still support bind_tools.

    Wrapping the model in Runnable.with_retry returned a RunnableRetry with no
    bind_tools -> "'RunnableRetry' object has no attribute 'bind_tools'" broke
    every tool-calling agent. The model must reach the handler un-wrapped.
    """
    from app.agents.leonardo.rails_agent import middleware as mw
    monkeypatch.setattr(mw, "get_llm", lambda name: _ToolBindableModel())

    seen = {}

    def handler(req):
        seen["model"] = req.model
        return "OK"

    out = mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler)
    assert out == "OK"
    assert hasattr(seen["model"], "bind_tools")  # NOT a RunnableRetry


def test_wrap_model_call_preserves_bind_tools_async(monkeypatch):
    """Same regression guard for the async (websocket) path."""
    import asyncio
    from app.agents.leonardo.rails_agent import middleware as mw
    monkeypatch.setattr(mw, "get_llm", lambda name: _ToolBindableModel())

    seen = {}

    async def handler(req):
        seen["model"] = req.model
        return "OK"

    out = asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(_Req(), handler))
    assert out == "OK"
    assert hasattr(seen["model"], "bind_tools")


def test_wrap_model_call_retries_transient_then_succeeds(monkeypatch):
    """A transient failure is retried (handler re-called) until it succeeds."""
    from app.agents.leonardo.rails_agent import middleware as mw
    monkeypatch.setattr(mw, "get_llm", lambda name: _ToolBindableModel())
    monkeypatch.setattr(mw.time, "sleep", lambda *_: None)  # no real backoff

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("transient blip")
        return "OK"

    out = mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler)
    assert out == "OK"
    assert calls["n"] == 3


def test_wrap_model_call_does_NOT_retry_deterministic(monkeypatch):
    """cache_control / image_url style errors must fail fast, not retry."""
    from app.agents.leonardo.rails_agent import middleware as mw
    monkeypatch.setattr(mw, "get_llm", lambda name: _ToolBindableModel())
    monkeypatch.setattr(mw.time, "sleep", lambda *_: None)

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise TypeError("unexpected keyword argument 'cache_control'")

    with pytest.raises(TypeError):
        mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler)
    assert calls["n"] == 1  # tried exactly once, no pointless retries


def test_wrap_model_call_stops_after_max_attempts(monkeypatch):
    """A persistently transient endpoint gives up (and re-raises) after the cap."""
    from app.agents.leonardo.rails_agent import middleware as mw
    monkeypatch.setattr(mw, "get_llm", lambda name: _ToolBindableModel())
    monkeypatch.setattr(mw.time, "sleep", lambda *_: None)

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        raise ConnectionError("still down")

    with pytest.raises(ConnectionError):
        mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler)
    assert calls["n"] == mw._MODEL_RETRY_MAX_ATTEMPTS
