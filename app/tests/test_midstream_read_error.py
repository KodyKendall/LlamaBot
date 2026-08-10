"""
Reproduction for the `ReadError` incident (fingerprint accf39ac3aa6, leo-puzero,
0.6.0c, rails_engineer_plan_mode_agent).

What happened: while the graph was being resumed after an `ask_user_question`
answer (`handle_question_response` -> `app.astream(Command(resume=...))`), the
provider dropped the socket **mid-stream**. httpcore raised `ReadError`, httpx
mapped it to `httpx.ReadError`, and it travelled all the way up through the
middleware stack and killed the turn.

Two distinct properties are pinned here:

  A. The mid-stream `httpx.ReadError` must be RETRIED by the async model-call
     path (`DynamicModelMiddleware.awrap_model_call`) — it is the same transient
     family as a connection drop. This is the crash itself.

  B. If it still escapes (retries exhausted), the error the user is shown must
     name the failure. `str(httpx.ReadError(...))` is the EMPTY STRING, so the
     websocket frame reads "Error resuming after question: " with nothing after
     the colon — the user is told something failed but not what, and the same
     blank message is what a support ticket arrives carrying.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState

from app.websocket.request_handler import RequestHandler


def make_midstream_read_error():
    """Build the exception exactly as the production traceback shows it.

    httpcore raises `ReadError` out of the anyio backend while iterating the
    response body; httpx's `map_httpcore_exceptions` re-raises it as
    `httpx.ReadError(str(exc))` — and `str(exc)` is empty.
    """
    import httpcore
    import httpx

    try:
        try:
            raise httpcore.ReadError()
        except httpcore.ReadError as exc:
            raise httpx.ReadError(str(exc)) from exc
    except httpx.ReadError as e:
        return e


def test_the_error_carries_no_message_at_all():
    """The premise both properties rest on: this exception is invisible in text."""
    exc = make_midstream_read_error()
    assert str(exc) == ""
    assert type(exc).__name__ == "ReadError"


# ---------------------------------------------------------------------------
# A. the crash: the async model-call path must retry it
# ---------------------------------------------------------------------------

class _ToolBindableModel:
    def bind_tools(self, *a, **k):
        return self


class _Req:
    def __init__(self):
        self.state = {"llm_model": "deepseek-v4-flash"}
        self.model = None

    def override(self, **kw):
        self.model = kw.get("model", self.model)
        return self


@pytest.mark.asyncio
async def test_async_model_call_retries_midstream_read_error(monkeypatch):
    """The websocket chat path (async) must survive a mid-stream socket drop.

    The sync retry loop has had a test since 0.5.3b; the async loop — the one the
    websocket path and this incident actually run through — had none.
    """
    from app.agents.leonardo.rails_agent import middleware as mw
    monkeypatch.setattr(mw, "get_llm", lambda name: _ToolBindableModel())
    monkeypatch.setattr(mw.asyncio, "sleep", AsyncMock())  # no real backoff

    calls = {"n": 0}

    async def handler(req):
        """Stands in for _execute_model_async: fails while consuming the stream."""
        calls["n"] += 1
        if calls["n"] < 3:
            raise make_midstream_read_error()
        return "OK"

    out = await mw.DynamicModelMiddleware().awrap_model_call(_Req(), handler)

    assert out == "OK"
    assert calls["n"] == 3, "mid-stream ReadError was not retried — the turn dies"


@pytest.mark.asyncio
async def test_read_error_raised_while_iterating_a_stream_is_retried(monkeypatch):
    """Same thing, with the failure raised from an async iterator (as in prod).

    The exception surfaces *inside* the handler while chunks are being consumed,
    not from the request call — so it must still be caught by the retry loop.
    """
    from app.agents.leonardo.rails_agent import middleware as mw
    monkeypatch.setattr(mw, "get_llm", lambda name: _ToolBindableModel())
    monkeypatch.setattr(mw.asyncio, "sleep", AsyncMock())

    attempts = {"n": 0}

    async def response_body(fail: bool):
        yield "partial "
        if fail:
            raise make_midstream_read_error()
        yield "answer"

    async def handler(req):
        attempts["n"] += 1
        text = ""
        async for chunk in response_body(fail=attempts["n"] == 1):
            text += chunk
        return text

    out = await mw.DynamicModelMiddleware().awrap_model_call(_Req(), handler)

    assert out == "partial answer"
    assert attempts["n"] == 2


# ---------------------------------------------------------------------------
# B. the fallout: what the user is actually shown when it escapes
# ---------------------------------------------------------------------------

def test_describe_exception_keeps_the_class_name_when_there_is_no_message():
    from app.websocket.error_text import describe_exception

    assert describe_exception(make_midstream_read_error()) == "ReadError"
    assert describe_exception(TypeError("bad kwarg 'cache_control'")) == (
        "TypeError: bad kwarg 'cache_control'"
    )
    # Whitespace-only is as useless as empty — treat it the same.
    assert describe_exception(RuntimeError("  \n ")) == "RuntimeError"


@pytest.mark.asyncio
async def test_telemetry_message_is_not_blank_but_fingerprint_is_unchanged():
    """The mothership row must name the error; its fingerprint must NOT move.

    Re-fingerprinting on a changed error_message would split every existing
    incident's history in two, so the hash still uses the raw first line.
    """
    import hashlib

    captured = {}

    class _FakeMothership:
        async def report_error(self, **kwargs):
            captured.update(kwargs)

    app = MagicMock()
    app.state.mothership_client = _FakeMothership()
    handler = RequestHandler.__new__(RequestHandler)  # skip FastAPI __init__
    handler.app = app

    try:
        raise make_midstream_read_error()
    except Exception as e:
        await handler._report_error_to_mothership(
            e, {"thread_id": "77d11f98", "agent_name": "rails_engineer_plan_mode_agent"}
        )

    assert captured["error_class"] == "ReadError"
    assert captured["error_message"] == "ReadError"      # was: "" (a blank cell)
    assert captured["fingerprint"] == hashlib.md5(
        b"ReadError||rails_engineer_plan_mode_agent"
    ).hexdigest()


def _connected_websocket():
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


def _graph_that_dies_midstream():
    """A compiled-graph stand-in whose astream raises ReadError mid-stream."""
    app = MagicMock()
    app.aget_state = AsyncMock(return_value=MagicMock(tasks=[]))

    async def astream(*a, **kw):
        raise make_midstream_read_error()
        yield  # pragma: no cover - makes this an async generator

    app.astream = astream
    return app


@pytest.mark.asyncio
async def test_question_resume_error_frame_names_the_failure():
    """`Error resuming after question: ` + str(exc) is blank for ReadError."""
    handler = RequestHandler(MagicMock())
    websocket = _connected_websocket()
    graph = _graph_that_dies_midstream()

    with patch.object(handler, "get_langgraph_app_and_state",
                      return_value=(graph, {}, {})), \
         patch.object(handler, "_repair_thread_state_if_needed", AsyncMock()), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        with pytest.raises(Exception):
            await handler.handle_question_response(
                {"thread_id": "77d11f98", "agent_name": "rails_engineer_plan_mode_agent",
                 "answer": "yes"},
                websocket,
            )

    frames = [c.args[0] for c in websocket.send_json.await_args_list]
    errors = [f for f in frames if f.get("type") == "error"]
    assert errors, "no error frame was sent to the browser"
    content = errors[-1]["content"]
    assert "ReadError" in content, (
        f"user is shown a blank cause: {content!r} — the exception type is the "
        "only information this failure carries"
    )


@pytest.mark.asyncio
async def test_approval_resume_error_frame_names_the_failure():
    """The approval resume path has the identical blank-message bug."""
    handler = RequestHandler(MagicMock())
    websocket = _connected_websocket()
    graph = _graph_that_dies_midstream()

    with patch.object(handler, "get_langgraph_app_and_state",
                      return_value=(graph, {}, {})), \
         patch.object(handler, "_repair_thread_state_if_needed", AsyncMock()), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        with pytest.raises(Exception):
            await handler.handle_approval_response(
                {"thread_id": "77d11f98", "agent_name": "rails_engineer_plan_mode_agent",
                 "decision": "approve"},
                websocket,
            )

    frames = [c.args[0] for c in websocket.send_json.await_args_list]
    errors = [f for f in frames if f.get("type") == "error"]
    assert errors, "no error frame was sent to the browser"
    assert "ReadError" in errors[-1]["content"], (
        f"user is shown a blank cause: {errors[-1]['content']!r}"
    )
