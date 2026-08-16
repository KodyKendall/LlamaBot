"""Tests for shipping performance telemetry to the mothership.

Two channels, both fail-open like every other MothershipClient method — a
telemetry hiccup must never surface to the user or break a turn:

* ``report_message(timings=...)`` — per-assistant-message timing, so every
  reply in the mothership carries the tokens/sec of the call that produced it.
  Rides the existing endpoint: no new route, no `source` allowlist problem.
* ``report_turn_metrics(...)`` — the end-of-turn rollup (where the wall clock
  went). New endpoint; contract in docs/handoff_mothership_performance.md.

See docs/dev/performance_telemetry.md.
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

SAMPLE_TIMINGS = {"duration_ms": 5000, "ttft_ms": 2000, "tokens_per_second": 100.0}

SAMPLE_METRICS = {
    "total_ms": 12480,
    "ttft_ms": 1830,
    "model_ms": 9200,
    "tool_ms": 2600,
    "overhead_ms": 680,
    "model_calls": 3,
    "tool_calls": 4,
    "output_tokens": 742,
    "input_tokens": 48120,
    "tokens_per_second": 96.4,
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


def _patched_http(fake_post):
    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)
    return mock_http


@pytest.mark.asyncio
async def test_report_message_forwards_timings():
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"ok": True})

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_patched_http(fake_post)):
        await client.report_message(
            thread_id="thread-abc",
            role="assistant",
            content="hello",
            sent_at="2026-08-08T00:00:00+00:00",
            timings=SAMPLE_TIMINGS,
        )

    assert captured["payload"]["timings"] == SAMPLE_TIMINGS


@pytest.mark.asyncio
async def test_report_message_omits_timings_when_absent():
    # Older mothership builds must keep working, and a turn with no measurable
    # model call should not post an empty key.
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response()

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_patched_http(fake_post)):
        await client.report_message(
            thread_id="t", role="user", content="hi", sent_at="2026-08-08T00:00:00+00:00"
        )

    assert "timings" not in captured["payload"]


@pytest.mark.asyncio
async def test_report_turn_metrics_posts_the_rollup():
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return _mock_response()

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_patched_http(fake_post)):
        await client.report_turn_metrics(
            thread_id="thread-abc",
            metrics=SAMPLE_METRICS,
            agent_mode="rails_agent",
            model="deepseek-v4-flash",
            llamabot_version="0.6.1",
            occurred_at="2026-08-08T00:00:00+00:00",
        )

    assert captured["url"].endswith("/api/leonardo/report_turn_metrics")
    assert captured["headers"]["Authorization"] == "Bearer tok-test"
    payload = captured["payload"]
    assert payload["instance_name"] == "test-instance"
    assert payload["thread_id"] == "thread-abc"
    assert payload["agent_mode"] == "rails_agent"
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["metrics"] == SAMPLE_METRICS


@pytest.mark.asyncio
async def test_report_turn_metrics_never_raises_on_transport_error():
    # Fail-open contract: the turn already finished and the user has their
    # answer. A metrics failure must be invisible to them.
    client = _make_client()

    async def fake_post(url, *, json, headers):
        raise httpx.ConnectError("mothership unreachable")

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_patched_http(fake_post)):
        assert await client.report_turn_metrics(thread_id="t", metrics=SAMPLE_METRICS) is None


@pytest.mark.asyncio
async def test_report_turn_metrics_never_raises_on_http_error():
    client = _make_client()
    resp = _mock_response(status=404)
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found", request=MagicMock(), response=resp
    )
    resp.text = "no such route"

    async def fake_post(url, *, json, headers):
        return resp

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_patched_http(fake_post)):
        assert await client.report_turn_metrics(thread_id="t", metrics=SAMPLE_METRICS) is None


@pytest.mark.asyncio
async def test_report_turn_metrics_is_disabled_without_config():
    # Self-hosted boxes have no instance.json; they must post nothing at all.
    client = MothershipClient.__new__(MothershipClient)
    client.config = None

    with patch("app.services.mothership_client.httpx.AsyncClient") as http:
        assert await client.report_turn_metrics(thread_id="t", metrics=SAMPLE_METRICS) is None
        http.assert_not_called()


@pytest.mark.asyncio
async def test_report_turn_metrics_skips_empty_metrics():
    # A turn that recorded nothing (interrupt before any model call) has
    # nothing to say; posting an empty row would pollute the fleet averages.
    client = _make_client()

    with patch("app.services.mothership_client.httpx.AsyncClient") as http:
        assert await client.report_turn_metrics(thread_id="t", metrics={}) is None
        http.assert_not_called()
