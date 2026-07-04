"""
Unit tests for MothershipClient.report_error — instance→mothership error telemetry.

Mirrors the report_disconnect contract: fire-and-forget POST to
/api/leonardo/report_error with a Bearer token, never raises, no-op when the
mothership integration is disabled. This is the hook that ends the visibility
gap — an end user hitting an error now surfaces to the LlamaPress team with the
model/agent_mode/version needed to triage it (see docs/dev/error_telemetry.md).
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
async def test_report_error_payload_and_headers():
    """Correct URL, Bearer header, and the triage fields the team needs."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return _mock_response(body={"ok": True})

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        await client.report_error(
            thread_id="thread-abc",
            error_class="TypeError",
            error_message="Completions.create() got an unexpected keyword argument 'cache_control'",
            traceback_str="Traceback (most recent call last): ...",
            agent_mode="rails_ai_builder",
            model="deepseek-v4-flash",
            llamabot_version="0.3.4",
            occurred_at="2026-07-03T00:00:00+00:00",
            fingerprint="abc123",
            recovered=True,
        )

    assert captured["url"] == "https://mothership.example.com/api/leonardo/report_error"
    assert captured["headers"]["Authorization"] == "Bearer tok-test"
    p = captured["payload"]
    assert p["instance_name"] == "test-instance"
    assert p["thread_id"] == "thread-abc"
    assert p["error_class"] == "TypeError"
    assert "cache_control" in p["error_message"]
    assert p["traceback"].startswith("Traceback")
    assert p["agent_mode"] == "rails_ai_builder"
    assert p["model"] == "deepseek-v4-flash"
    assert p["llamabot_version"] == "0.3.4"
    assert p["fingerprint"] == "abc123"
    assert p["recovered"] is True


@pytest.mark.asyncio
async def test_report_error_truncates_large_fields():
    """error_message and traceback are bounded so a huge trace can't bloat the POST."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"ok": True})

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        await client.report_error(
            thread_id="t",
            error_class="ValueError",
            error_message="x" * 9000,
            traceback_str="y" * 99000,
        )

    p = captured["payload"]
    assert len(p["error_message"]) <= 2000
    assert len(p["traceback"]) <= 5000


@pytest.mark.asyncio
async def test_report_error_omits_unset_optional_fields():
    """Optional metadata absent when not provided (no null spam)."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"ok": True})

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        await client.report_error(
            thread_id="t",
            error_class="ValueError",
            error_message="boom",
            traceback_str="tb",
        )

    p = captured["payload"]
    for k in ("agent_mode", "model", "llamabot_version", "occurred_at", "fingerprint", "recovered"):
        assert k not in p


@pytest.mark.asyncio
async def test_report_error_returns_none_on_http_error():
    """An HTTP 500 must be swallowed — telemetry never worsens the user's error."""
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
        result = await client.report_error(
            thread_id="t", error_class="E", error_message="m", traceback_str="tb"
        )

    assert result is None


@pytest.mark.asyncio
async def test_report_error_returns_none_on_network_error():
    """A network error must be swallowed — never raises."""
    client = _make_client()

    async def fake_post(url, *, json, headers):
        raise httpx.RequestError("connection refused", request=MagicMock())

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        result = await client.report_error(
            thread_id="t", error_class="E", error_message="m", traceback_str="tb"
        )

    assert result is None


@pytest.mark.asyncio
async def test_report_error_disabled_client_is_noop():
    """When mothership integration is disabled, report_error is a silent no-op."""
    client = MothershipClient.__new__(MothershipClient)
    client.config = None  # disabled

    result = await client.report_error(
        thread_id="t", error_class="E", error_message="m", traceback_str="tb"
    )
    assert result is None


# --- wiring: RequestHandler._report_error_to_mothership ----------------------

def _handler_with_mothership(mothership):
    from app.websocket.request_handler import RequestHandler

    class _State:
        pass

    class _App:
        state = _State()

    app = _App()
    app.state.mothership_client = mothership
    h = RequestHandler.__new__(RequestHandler)  # skip FastAPI __init__
    h.app = app
    return h


@pytest.mark.asyncio
async def test_handler_reports_error_with_resolved_fields():
    """The catch-site helper resolves thread/model/agent_mode and a fingerprint."""
    captured = {}

    class _FakeMothership:
        async def report_error(self, **kwargs):
            captured.update(kwargs)

    h = _handler_with_mothership(_FakeMothership())
    try:
        raise TypeError("unexpected keyword argument 'cache_control'")
    except TypeError as e:
        await h._report_error_to_mothership(e, {
            "thread_id": "t1",
            "agent_name": "rails_ai_builder",
            "llm_model": "deepseek-v4-flash",
        })

    assert captured["error_class"] == "TypeError"
    assert captured["thread_id"] == "t1"
    assert captured["agent_mode"] == "rails_ai_builder"
    assert captured["model"] == "deepseek-v4-flash"
    assert "cache_control" in captured["error_message"]
    assert captured["fingerprint"]                 # non-empty
    assert captured["recovered"] is False          # raw error still shown to user
    assert captured["traceback_str"]               # real traceback captured


@pytest.mark.asyncio
async def test_handler_report_error_is_noop_without_mothership():
    """No mothership configured → helper is a silent no-op, never raises."""
    h = _handler_with_mothership(None)
    await h._report_error_to_mothership(TypeError("x"), {})  # must not raise


@pytest.mark.asyncio
async def test_handler_report_error_swallows_reporting_failure():
    """A failure inside reporting must never propagate out of the helper."""
    class _BoomMothership:
        async def report_error(self, **kwargs):
            raise RuntimeError("mothership exploded")

    h = _handler_with_mothership(_BoomMothership())
    # must not raise despite the mothership blowing up
    await h._report_error_to_mothership(ValueError("y"), {"thread_id": "t"})
