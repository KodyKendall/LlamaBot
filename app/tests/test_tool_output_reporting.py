"""
Tests for ToolMessage (tool call output) reporting to the mothership.

Sequel to 0.5.1m: we already report assistant tool_calls (actions); this
adds the observations — what each tool actually returned — so the mothership
can reconstruct full agent trajectories.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from langchain_core.messages import ToolMessage as LCToolMessage

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


def _patch_http(captured: dict):
    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"ok": True})

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)
    return mock_http


# ---------------------------------------------------------------------------
# MothershipClient.report_message — tool_call_id wire contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_message_tool_role_includes_tool_call_id():
    """role='tool' payload must include tool_call_id."""
    client = _make_client()
    captured = {}
    mock_http = _patch_http(captured)

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.report_message(
            thread_id="thread-abc",
            role="tool",
            content="file contents verbatim",
            sent_at="2026-06-21T00:00:00+00:00",
            tool_call_id="call_abc123",
        )

    assert captured["payload"]["role"] == "tool"
    assert captured["payload"]["tool_call_id"] == "call_abc123"


@pytest.mark.asyncio
async def test_report_message_tool_role_preserves_full_content():
    """content must be sent verbatim — not truncated."""
    client = _make_client()
    captured = {}
    mock_http = _patch_http(captured)

    large_output = "x" * 10_000  # 10 KB of raw tool output

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.report_message(
            thread_id="thread-abc",
            role="tool",
            content=large_output,
            sent_at="2026-06-21T00:00:00+00:00",
            tool_call_id="call_abc123",
        )

    assert captured["payload"]["content"] == large_output


@pytest.mark.asyncio
async def test_report_message_omits_tool_call_id_when_none():
    """When tool_call_id is None (e.g. assistant role), the key must be absent."""
    client = _make_client()
    captured = {}
    mock_http = _patch_http(captured)

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.report_message(
            thread_id="thread-abc",
            role="assistant",
            content="hello",
            sent_at="2026-06-21T00:00:00+00:00",
        )

    assert "tool_call_id" not in captured["payload"]


@pytest.mark.asyncio
async def test_report_message_tool_calls_carried_with_tool_role():
    """tool_calls list on a tool-role report carries name + tool_call_id for self-describing rows."""
    client = _make_client()
    captured = {}
    mock_http = _patch_http(captured)

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        await client.report_message(
            thread_id="thread-abc",
            role="tool",
            content="result",
            sent_at="2026-06-21T00:00:00+00:00",
            tool_call_id="call_abc123",
            tool_calls=[{"name": "bash_command", "tool_call_id": "call_abc123"}],
        )

    assert captured["payload"]["tool_calls"] == [{"name": "bash_command", "tool_call_id": "call_abc123"}]


# ---------------------------------------------------------------------------
# request_handler integration: ToolMessage in update stream → report fired
# ---------------------------------------------------------------------------

def _make_tool_message(content="cmd output", name="bash_command", tool_call_id="call_xyz"):
    return LCToolMessage(content=content, name=name, tool_call_id=tool_call_id)


@pytest.mark.asyncio
async def test_tool_message_in_update_triggers_report(monkeypatch):
    """A ToolMessage surfacing in the update stream must fire one report_message call."""
    from app.websocket import request_handler as rh

    tool_msg = _make_tool_message()
    report_calls = []

    async def fake_report(**kwargs):
        report_calls.append(kwargs)

    mock_mothership = MagicMock()
    mock_mothership.report_message = fake_report

    # Call the internal helper directly to avoid wiring up the full WebSocket stack.
    await rh._report_tool_messages(
        messages=[tool_msg],
        mothership=mock_mothership,
        thread_id="thread-123",
        agent_depth=0,
    )

    assert len(report_calls) == 1
    call = report_calls[0]
    assert call["role"] == "tool"
    assert call["content"] == "cmd output"
    assert call["tool_call_id"] == "call_xyz"


@pytest.mark.asyncio
async def test_tool_message_report_no_double_report(monkeypatch):
    """A single ToolMessage produces exactly one report, not two."""
    from app.websocket import request_handler as rh

    tool_msg = _make_tool_message()
    report_calls = []

    async def fake_report(**kwargs):
        report_calls.append(kwargs)

    mock_mothership = MagicMock()
    mock_mothership.report_message = fake_report

    await rh._report_tool_messages(
        messages=[tool_msg],
        mothership=mock_mothership,
        thread_id="thread-123",
        agent_depth=0,
    )

    assert len(report_calls) == 1


@pytest.mark.asyncio
async def test_tool_message_report_non_tool_messages_ignored(monkeypatch):
    """Non-ToolMessage entries in the messages list must not trigger a report."""
    from app.websocket import request_handler as rh
    from langchain_core.messages import AIMessage

    report_calls = []

    async def fake_report(**kwargs):
        report_calls.append(kwargs)

    mock_mothership = MagicMock()
    mock_mothership.report_message = fake_report

    ai_msg = AIMessage(content="hello")
    await rh._report_tool_messages(
        messages=[ai_msg],
        mothership=mock_mothership,
        thread_id="thread-123",
        agent_depth=0,
    )

    assert len(report_calls) == 0


@pytest.mark.asyncio
async def test_subagent_tool_messages_are_reported(monkeypatch):
    """Sub-agent tool outputs (agent_depth > 0) must be reported (tagged with agent_depth)."""
    from app.websocket import request_handler as rh

    tool_msg = _make_tool_message()
    report_calls = []

    async def fake_report(**kwargs):
        report_calls.append(kwargs)

    mock_mothership = MagicMock()
    mock_mothership.report_message = fake_report

    await rh._report_tool_messages(
        messages=[tool_msg],
        mothership=mock_mothership,
        thread_id="thread-123",
        agent_depth=1,  # sub-agent
    )

    assert len(report_calls) == 1
    # agent_depth must be tagged in tool_calls for downstream filtering
    tool_calls_sent = report_calls[0].get("tool_calls", [])
    assert any(tc.get("agent_depth") == 1 for tc in tool_calls_sent)
