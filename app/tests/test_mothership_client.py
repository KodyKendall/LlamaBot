"""
Unit tests for MothershipClient.report_message — tool_calls forwarding.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.services.mothership_client import MothershipClient


FAKE_CONFIG = {
    "instance_name": "test-instance",
    "mothership_url": "https://mothership.example.com",
    "mothership_api_token": "tok-test",
    "lease_duration_seconds": 300,
}

SAMPLE_TOOL_CALLS = [
    {"name": "create_file", "args": {"path": "foo.rb", "content": "puts 1"}, "id": "tc-1"},
    {"name": "run_shell", "args": {"command": "ls"}, "id": "tc-2"},
]


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


@pytest.mark.asyncio
async def test_report_message_includes_tool_calls_in_payload():
    """When tool_calls is provided, the POSTed payload must include a tool_calls key."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"ok": True})

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.report_message(
            thread_id="thread-abc",
            role="assistant",
            content="",
            sent_at="2026-06-21T00:00:00+00:00",
            tool_calls=SAMPLE_TOOL_CALLS,
        )

    assert "tool_calls" in captured["payload"], "tool_calls must be present in the posted payload"
    assert captured["payload"]["tool_calls"] == SAMPLE_TOOL_CALLS


@pytest.mark.asyncio
async def test_report_message_omits_tool_calls_when_none():
    """When tool_calls is None, the payload must NOT include a tool_calls key."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"ok": True})

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.report_message(
            thread_id="thread-abc",
            role="assistant",
            content="hello",
            sent_at="2026-06-21T00:00:00+00:00",
            tool_calls=None,
        )

    assert "tool_calls" not in captured["payload"], "tool_calls must be absent from the payload when None"


@pytest.mark.asyncio
async def test_report_message_omits_tool_calls_when_empty_list():
    """When tool_calls is an empty list, the payload must NOT include a tool_calls key."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"ok": True})

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.report_message(
            thread_id="thread-abc",
            role="assistant",
            content="hello",
            sent_at="2026-06-21T00:00:00+00:00",
            tool_calls=[],
        )

    assert "tool_calls" not in captured["payload"]
