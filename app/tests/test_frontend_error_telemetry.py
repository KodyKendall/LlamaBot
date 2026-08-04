"""Tests for frontend (browser) error telemetry → mothership.

Error telemetry used to be backend-only: `request_handler._report_error_to_mothership`
only fires on a Python exception. A client-side socket drop raises nothing server-side,
so Kody's "Lost connection" mid-run on the mbc-preceptors thread (2026-07-24) produced
zero trace on the mothership — no InstanceError row at all.

The mothership receiver already accepts `source: "frontend"` (shipped 2026-07-24).
These tests pin the instance side: `report_error` carries the source, and the new
same-origin passthrough endpoint forwards, truncates, fingerprints, and never 500s the
browser.
"""

import hashlib

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


# --------------------------------------------------------------------------
# MothershipClient.report_error carries a source
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_error_includes_source_when_given():
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response()

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        await client.report_error(
            thread_id="t-1",
            error_class="FrontendConnectionLost",
            error_message="socket closed 1006",
            traceback_str="",
            source="frontend",
        )

    assert captured["payload"]["source"] == "frontend"


@pytest.mark.asyncio
async def test_report_error_defaults_source_to_llamabot():
    """Backend callers must keep working unchanged."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response()

    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=_mock_http(fake_post)):
        await client.report_error(
            thread_id="t-1",
            error_class="ValueError",
            error_message="boom",
            traceback_str="Traceback...",
        )

    assert captured["payload"]["source"] == "llamabot"


# --------------------------------------------------------------------------
# POST /api/frontend-error
# --------------------------------------------------------------------------

@pytest.fixture
def frontend_error_endpoint():
    """The endpoint function plus a stub mothership, bypassing HTTP/auth plumbing."""
    from app.routers import api

    calls = []

    class StubMothership:
        enabled = True

        async def report_error(self, **kwargs):
            calls.append(kwargs)
            return None

    request = MagicMock()
    request.app.state.mothership_client = StubMothership()
    return api, request, calls


def _body(api, **overrides):
    fields = dict(
        error_class="FrontendConnectionLost",
        error_message="Lost connection mid-run (thinking indicator active)",
        stack="at WebSocketManager.handleClose (WebSocketManager.js:88)",
        thread_id="1783365821191-5cmtxo5pc",
        agent_mode="rails_agent",
        model="deepseek-v4-flash",
    )
    fields.update(overrides)
    return api.FrontendErrorRequest(**fields)


@pytest.mark.asyncio
async def test_forwards_with_source_frontend(frontend_error_endpoint):
    api, request, calls = frontend_error_endpoint

    result = await api.api_report_frontend_error(request, _body(api), username="kody")

    assert result["success"] is True
    assert len(calls) == 1
    assert calls[0]["source"] == "frontend"
    assert calls[0]["error_class"] == "FrontendConnectionLost"
    assert calls[0]["thread_id"] == "1783365821191-5cmtxo5pc"
    assert calls[0]["agent_mode"] == "rails_agent"
    assert calls[0]["model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_computes_the_same_fingerprint_the_backend_uses(frontend_error_endpoint):
    """Matches request_handler.py so frontend + backend rows dedupe consistently."""
    api, request, calls = frontend_error_endpoint

    await api.api_report_frontend_error(request, _body(api), username="kody")

    expected = hashlib.md5(
        "FrontendConnectionLost|"
        "Lost connection mid-run (thinking indicator active)|"
        "rails_agent".encode("utf-8", "replace")
    ).hexdigest()
    assert calls[0]["fingerprint"] == expected


@pytest.mark.asyncio
async def test_client_supplied_fingerprint_wins(frontend_error_endpoint):
    api, request, calls = frontend_error_endpoint

    await api.api_report_frontend_error(
        request, _body(api, fingerprint="deadbeef"), username="kody"
    )
    assert calls[0]["fingerprint"] == "deadbeef"


@pytest.mark.asyncio
async def test_truncates_to_mothership_limits(frontend_error_endpoint):
    api, request, calls = frontend_error_endpoint

    await api.api_report_frontend_error(
        request,
        # Over the per-field limits but under the total-size guard.
        _body(api, error_message="m" * 3000, stack="s" * 9000),
        username="kody",
    )

    assert len(calls[0]["error_message"]) <= 2000
    assert len(calls[0]["traceback_str"]) <= 5000


@pytest.mark.asyncio
async def test_rejects_oversized_bodies(frontend_error_endpoint):
    """Light abuse guard — a runaway page must not fire 100KB payloads at us."""
    api, request, calls = frontend_error_endpoint

    result = await api.api_report_frontend_error(
        request, _body(api, stack="x" * 60000), username="kody"
    )

    assert result["success"] is False
    assert calls == [], "oversized report should not reach the mothership"


@pytest.mark.asyncio
async def test_never_raises_when_the_mothership_errors(frontend_error_endpoint):
    """A reporting hiccup must never 500 the browser."""
    api, request, _calls = frontend_error_endpoint

    class ExplodingMothership:
        enabled = True

        async def report_error(self, **kwargs):
            raise RuntimeError("mothership down")

    request.app.state.mothership_client = ExplodingMothership()

    result = await api.api_report_frontend_error(request, _body(api), username="kody")
    assert result["success"] is False


@pytest.mark.asyncio
async def test_no_op_when_mothership_disabled(frontend_error_endpoint):
    api, request, calls = frontend_error_endpoint

    class DisabledMothership:
        enabled = False

        async def report_error(self, **kwargs):  # pragma: no cover
            calls.append(kwargs)

    request.app.state.mothership_client = DisabledMothership()

    result = await api.api_report_frontend_error(request, _body(api), username="kody")
    assert result["success"] is False
    assert calls == []


# --------------------------------------------------------------------------
# Dead code removal
# --------------------------------------------------------------------------

def test_report_disconnect_is_gone():
    """No caller in app/, and the mothership has no /report_disconnect route.

    It was scaffolded and never wired on either end; FrontendConnectionLost
    supersedes its purpose.
    """
    assert not hasattr(MothershipClient, "report_disconnect")
