"""
Unit tests for MothershipClient.submit_feedback — end-user 👍/👎 reporting.

Mirrors the report_message contract: POST to /api/leonardo/submit_feedback with a
Bearer token, never raises, returns None on any failure.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.services.mothership_client import MothershipClient, TELEMETRY_DISABLED_ENV


FAKE_CONFIG = {
    "instance_name": "test-instance",
    "mothership_url": "https://mothership.example.com",
    "mothership_api_token": "tok-test",
    "lease_duration_seconds": 300,
}


@pytest.fixture(autouse=True)
def _reporting_not_suppressed(monkeypatch):
    """These tests assert the reported payloads themselves, so the suite-wide
    telemetry kill switch (app/tests/conftest.py) has to be off for them. httpx
    is patched in every test below, so nothing leaves the process either way."""
    monkeypatch.delenv(TELEMETRY_DISABLED_ENV, raising=False)


def _make_client() -> MothershipClient:
    client = MothershipClient.__new__(MothershipClient)
    client.config = FAKE_CONFIG
    return client


def _mock_response(status=200, body=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.raise_for_status = MagicMock()
    return resp


def _mock_http(post_side_effect):
    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=post_side_effect)
    return mock_http


@pytest.mark.asyncio
async def test_submit_feedback_message_scope_payload_and_headers():
    """scope=message: correct URL, Bearer header, and required body fields."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return _mock_response(body={"success": True, "annotation_id": 1, "message_id": 2})

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        result = await client.submit_feedback(
            thread_id="thread-abc",
            rating="bad",
            scope="message",
            note="this was wrong",
            content="raw markdown of the message",
            sent_at="2026-06-29T00:00:00+00:00",
        )

    assert result == {"success": True, "annotation_id": 1, "message_id": 2}
    assert captured["url"] == "https://mothership.example.com/api/leonardo/submit_feedback"
    assert captured["headers"]["Authorization"] == "Bearer tok-test"
    p = captured["payload"]
    assert p["instance_name"] == "test-instance"
    assert p["thread_id"] == "thread-abc"
    assert p["rating"] == "bad"
    assert p["scope"] == "message"
    assert p["note"] == "this was wrong"
    assert p["content"] == "raw markdown of the message"
    assert p["sent_at"] == "2026-06-29T00:00:00+00:00"


@pytest.mark.asyncio
async def test_submit_feedback_session_scope_omits_optional_fields():
    """scope=session with no note/content/sent_at: those keys are absent."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"success": True})

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        await client.submit_feedback(thread_id="thread-xyz", rating="good", scope="session")

    p = captured["payload"]
    assert p["scope"] == "session"
    assert p["rating"] == "good"
    assert p["thread_id"] == "thread-xyz"
    assert "note" not in p
    assert "content" not in p
    assert "sent_at" not in p


@pytest.mark.asyncio
async def test_submit_feedback_defaults_to_message_scope():
    """scope defaults to 'message' when not provided."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"success": True})

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        await client.submit_feedback(thread_id="t", rating="good")

    assert captured["payload"]["scope"] == "message"


@pytest.mark.asyncio
async def test_submit_feedback_returns_none_on_http_error():
    """An HTTP 500 must be swallowed → returns None, never raises."""
    client = _make_client()

    def raise_500():
        raise httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(status_code=500, text="boom")
        )

    async def fake_post(url, *, json, headers):
        resp = _mock_response(status=500)
        resp.raise_for_status = MagicMock(side_effect=raise_500)
        return resp

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        result = await client.submit_feedback(thread_id="t", rating="bad")

    assert result is None


@pytest.mark.asyncio
async def test_submit_feedback_returns_none_on_network_error():
    """A network error must be swallowed → returns None, never raises."""
    client = _make_client()

    async def fake_post(url, *, json, headers):
        raise httpx.RequestError("connection refused", request=MagicMock())

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        result = await client.submit_feedback(thread_id="t", rating="good")

    assert result is None


@pytest.mark.asyncio
async def test_submit_feedback_disabled_client_returns_none():
    """When mothership integration is disabled, submit_feedback is a no-op."""
    client = MothershipClient.__new__(MothershipClient)
    client.config = None  # disabled

    result = await client.submit_feedback(thread_id="t", rating="good")
    assert result is None
