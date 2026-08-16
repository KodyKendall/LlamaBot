"""Feedback debug-context passthrough + WebSocket log correlation.

Lohman's 2026-07-06 thumbs-down ("Seem like after every ticket is created, the
connection is lost") arrived with only thread_id/rating/scope/note. Support could not
tell a network drop from a hung run, because nothing about the websocket, the model, or
the browser's recent console output was captured — and the backend's own websocket
open/close logs did not carry thread_id either, so they couldn't be correlated afterwards.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.mothership_client import TELEMETRY_DISABLED_ENV


# --------------------------------------------------------------------------
# /api/feedback carries debug_context through to the mothership
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reporting_not_suppressed(monkeypatch):
    """These tests assert the reported payloads themselves, so the suite-wide
    telemetry kill switch (app/tests/conftest.py) has to be off for them. httpx
    is patched in every test below, so nothing leaves the process either way."""
    monkeypatch.delenv(TELEMETRY_DISABLED_ENV, raising=False)


@pytest.fixture
def feedback_endpoint():
    from app.routers import api

    calls = []

    class StubMothership:
        enabled = True
        reporting_enabled = True

        async def submit_feedback(self, **kwargs):
            calls.append(kwargs)
            return {"success": True}

    request = MagicMock()
    request.app.state.mothership_client = StubMothership()
    return api, request, calls


SNAPSHOT = {
    "thread_id": "1783365821191-5cmtxo5pc",
    "captured_at": "2026-07-06T19:35:05Z",
    "agent_mode": "rails_ticket_mode_agent",
    "llm_model": "deepseek-v4-flash",
    "connection": {
        "ready_state": 3,
        "last_close": {"code": 1006, "reason": "", "wasClean": False},
        "reconnect_attempts": 2,
        "outbox_length": 1,
    },
    "recent_events": [
        {"at": "2026-07-06T19:34:58Z", "event": "websocket_close", "code": "1006"},
    ],
}


@pytest.mark.asyncio
async def test_debug_context_is_forwarded(feedback_endpoint):
    api, request, calls = feedback_endpoint

    body = api.FeedbackRequest(
        thread_id="1783365821191-5cmtxo5pc",
        rating="bad",
        note="Seem like after every ticket is created, the connection is lost",
        debug_context=SNAPSHOT,
    )
    result = await api.api_submit_feedback(request, body, username="lohman")

    assert result["success"] is True
    assert calls[0]["debug_context"]["connection"]["last_close"]["code"] == 1006
    assert calls[0]["debug_context"]["agent_mode"] == "rails_ticket_mode_agent"


@pytest.mark.asyncio
async def test_debug_context_is_optional(feedback_endpoint):
    """Older frontends (and the session banner before it's wired) send none."""
    api, request, calls = feedback_endpoint

    body = api.FeedbackRequest(thread_id="t-1", rating="good")
    result = await api.api_submit_feedback(request, body, username="kody")

    assert result["success"] is True
    assert calls[0]["debug_context"] is None


@pytest.mark.asyncio
async def test_oversized_debug_context_is_dropped_not_rejected(feedback_endpoint):
    """A bloated snapshot must never cost the user their feedback."""
    api, request, calls = feedback_endpoint

    body = api.FeedbackRequest(
        thread_id="t-1",
        rating="bad",
        debug_context={"recent_events": [{"message": "x" * 400} for _ in range(2000)]},
    )
    result = await api.api_submit_feedback(request, body, username="kody")

    assert result["success"] is True, "the rating itself must still land"
    assert calls[0]["debug_context"] is None


@pytest.mark.asyncio
async def test_submit_feedback_client_sends_debug_context():
    """MothershipClient must actually put it on the wire."""
    import httpx
    from unittest.mock import AsyncMock, patch
    from app.services.mothership_client import MothershipClient

    client = MothershipClient.__new__(MothershipClient)
    client.config = {
        "instance_name": "test-instance",
        "mothership_url": "https://mothership.example.com",
        "mothership_api_token": "tok",
    }

    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {"success": True}
        resp.raise_for_status = MagicMock()
        return resp

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.submit_feedback(
            thread_id="t-1", rating="bad", debug_context=SNAPSHOT
        )

    assert captured["payload"]["debug_context"]["connection"]["reconnect_attempts"] == 2


@pytest.mark.asyncio
async def test_no_debug_context_key_when_absent():
    """Don't send a null field to a receiver that may not know it yet."""
    import httpx
    from unittest.mock import AsyncMock, patch
    from app.services.mothership_client import MothershipClient

    client = MothershipClient.__new__(MothershipClient)
    client.config = {
        "instance_name": "i",
        "mothership_url": "https://m.example.com",
        "mothership_api_token": "tok",
    }
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {"success": True}
        resp.raise_for_status = MagicMock()
        return resp

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.submit_feedback(thread_id="t-1", rating="good")

    assert "debug_context" not in captured["payload"]


# --------------------------------------------------------------------------
# WebSocket log correlation
# --------------------------------------------------------------------------

def test_handler_has_a_connection_id():
    """Every socket needs a stable id so open/close/error lines can be joined up."""
    from app.websocket.web_socket_handler import WebSocketHandler

    ws = MagicMock()
    handler = WebSocketHandler.__new__(WebSocketHandler)
    handler.websocket = ws
    WebSocketHandler._init_connection_id(handler)

    assert handler.connection_id
    assert len(handler.connection_id) >= 6

    other = WebSocketHandler.__new__(WebSocketHandler)
    other.websocket = MagicMock()
    WebSocketHandler._init_connection_id(other)
    assert other.connection_id != handler.connection_id


def test_log_prefix_includes_connection_id_and_thread():
    from app.websocket.web_socket_handler import WebSocketHandler

    handler = WebSocketHandler.__new__(WebSocketHandler)
    handler.connection_id = "abc123"
    handler.current_thread_id = None

    assert "abc123" in WebSocketHandler._log_ctx(handler)

    handler.current_thread_id = "1783365821191-5cmtxo5pc"
    ctx = WebSocketHandler._log_ctx(handler)
    assert "abc123" in ctx
    assert "1783365821191-5cmtxo5pc" in ctx


def test_thread_id_is_remembered_from_an_inbound_message():
    """Disconnect logs are useless without the thread the socket was serving."""
    from app.websocket.web_socket_handler import WebSocketHandler

    handler = WebSocketHandler.__new__(WebSocketHandler)
    handler.connection_id = "abc123"
    handler.current_thread_id = None

    WebSocketHandler._note_message_context(
        handler,
        {"thread_id": "t-42", "agent_name": "rails_agent", "llm_model": "deepseek-v4-flash"},
    )

    assert handler.current_thread_id == "t-42"
    assert handler.current_agent_name == "rails_agent"
    assert handler.current_llm_model == "deepseek-v4-flash"
    assert "t-42" in WebSocketHandler._log_ctx(handler)


def test_message_context_survives_a_frame_without_a_thread_id():
    """Pings and control frames must not wipe the correlation we already have."""
    from app.websocket.web_socket_handler import WebSocketHandler

    handler = WebSocketHandler.__new__(WebSocketHandler)
    handler.connection_id = "abc123"
    handler.current_thread_id = "t-42"
    handler.current_agent_name = "rails_agent"
    handler.current_llm_model = "deepseek-v4-flash"

    WebSocketHandler._note_message_context(handler, {"type": "ping"})

    assert handler.current_thread_id == "t-42"
