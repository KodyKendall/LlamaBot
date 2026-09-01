"""
Rails health reporting in the lease loop (SI#417).

The fleet heartbeat was sent by LlamaBot, so the mothership could not tell a
healthy box from one whose Rails app was completely down. crm-4 called the
mothership 12 times during a 10 minute outage on 2026-08-31.

These tests pin the three things that make the signal trustworthy:
  1. the probe is generous enough not to cry wolf (25s — a HEALTHY box measured
     10.045s because dev-mode class reloading pays the whole reload cost),
  2. health is reported on EVERY tick, including on an idle box that is not
     renewing its lease (that idle box is the blind spot being closed),
  3. the report is fail-open: it never breaks lease renewal, and a mothership
     that does not have the endpoint yet (404) is a no-op, not an error.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

import httpx

from app.services.mothership_client import MothershipClient, TELEMETRY_DISABLED_ENV
from app.services.lease_manager import LeaseManager


FAKE_CONFIG = {
    "instance_name": "crm-4",
    "mothership_url": "https://mothership.example.com",
    "mothership_api_token": "tok-test",
    "lease_duration_seconds": 300,
}


@pytest.fixture(autouse=True)
def _reporting_not_suppressed(monkeypatch):
    """These tests assert the reported payloads, so the suite-wide telemetry kill
    switch (conftest.py) has to be off. httpx is patched in every test, so
    nothing leaves the process either way."""
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


def _make_manager(mothership, *, last_activity_seconds_ago=99999):
    """A LeaseManager whose app.state.timestamp is that many seconds old."""
    app = MagicMock()
    app.state.timestamp = datetime.now(timezone.utc) - timedelta(
        seconds=last_activity_seconds_ago
    )
    return LeaseManager(app, mothership)


# --------------------------------------------------------------------------
# MothershipClient.report_rails_health
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_report_rails_health_posts_the_agreed_contract():
    """The mothership half is built against a fixed contract; do not drift."""
    client = _make_client()
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return _mock_response(body={"ok": True})

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value.post = fake_post
    with patch("httpx.AsyncClient", return_value=mock_ctx):
        await client.report_rails_health(rails_status=200, rails_ms=1370)

    assert captured["url"] == (
        "https://mothership.example.com/api/leonardo/report_health"
    )
    assert captured["headers"]["Authorization"] == "Bearer tok-test"

    payload = captured["payload"]
    assert payload["instance_name"] == "crm-4"
    assert payload["rails_status"] == 200
    assert payload["rails_ms"] == 1370
    # ISO 8601 UTC, and actually parseable.
    assert datetime.fromisoformat(payload["checked_at"].replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_report_rails_health_treats_404_as_a_noop():
    """A LlamaBot that ships before the mothership endpoint must not log errors."""
    client = _make_client()

    request = httpx.Request("POST", "https://mothership.example.com/x")
    response = httpx.Response(404, request=request)

    async def fake_post(url, *, json, headers):
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value.post = fake_post
    with patch("httpx.AsyncClient", return_value=mock_ctx):
        with patch("app.services.mothership_client.logger") as log:
            await client.report_rails_health(rails_status=200, rails_ms=10)

    assert not log.error.called, "404 is an expected older mothership, not an error"


@pytest.mark.asyncio
async def test_report_rails_health_silent_when_reporting_disabled(monkeypatch):
    """The telemetry kill switch must silence health reports."""
    monkeypatch.setenv(TELEMETRY_DISABLED_ENV, "1")
    client = _make_client()

    called = False

    async def fake_post(url, *, json, headers):
        nonlocal called
        called = True
        return _mock_response()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value.post = fake_post
    with patch("httpx.AsyncClient", return_value=mock_ctx):
        await client.report_rails_health(rails_status=200, rails_ms=10)

    assert called is False


# --------------------------------------------------------------------------
# LeaseManager._probe_rails
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_uses_a_25_second_timeout():
    """MANDATORY GUARD. A healthy box measured 10.045s on /up because dev-mode
    class reloading pays the whole reload. A short timeout would report healthy
    boxes as down several times an hour."""
    manager = _make_manager(MagicMock())
    captured = {}

    def fake_client(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        ctx = AsyncMock()
        ctx.__aenter__.return_value.get = AsyncMock(
            return_value=_mock_response(status=200)
        )
        return ctx

    with patch("httpx.AsyncClient", side_effect=fake_client):
        await manager._probe_rails()

    assert captured["timeout"] == 25.0


@pytest.mark.asyncio
async def test_probe_returns_status_and_duration():
    manager = _make_manager(MagicMock())

    ctx = AsyncMock()
    ctx.__aenter__.return_value.get = AsyncMock(return_value=_mock_response(status=200))
    with patch("httpx.AsyncClient", return_value=ctx):
        status, ms = await manager._probe_rails()

    assert status == 200
    assert isinstance(ms, int)
    assert ms >= 0


@pytest.mark.asyncio
async def test_probe_reports_zero_when_rails_does_not_answer():
    """A wedged Rails answers nothing at all, including /up. That is status 0,
    which is the whole point of the signal."""
    manager = _make_manager(MagicMock())

    ctx = AsyncMock()
    ctx.__aenter__.return_value.get = AsyncMock(
        side_effect=httpx.ConnectTimeout("wedged")
    )
    with patch("httpx.AsyncClient", return_value=ctx):
        status, ms = await manager._probe_rails()

    assert status == 0
    assert isinstance(ms, int)


# --------------------------------------------------------------------------
# The loop wiring — the actual blind spot
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_box_still_reports_health():
    """THE BUG. Lease renewal is gated on ACTIVITY_THRESHOLD (600s). If the health
    report lived inside _check_and_renew, an idle box would stop reporting, which
    is exactly the blind spot being closed. Idle here = 99999s since activity."""
    mothership = MagicMock()
    mothership.enabled = True
    mothership.renew_lease = AsyncMock(return_value=None)
    mothership.report_rails_health = AsyncMock(return_value=None)

    manager = _make_manager(mothership, last_activity_seconds_ago=99999)
    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(200, 1370))):
        await manager._tick()

    assert mothership.report_rails_health.await_count == 1, (
        "an idle box must still report Rails health"
    )
    assert mothership.renew_lease.await_count == 0, (
        "an idle box must NOT renew its lease (unchanged behaviour)"
    )


@pytest.mark.asyncio
async def test_health_report_failure_does_not_break_lease_renewal():
    """Lease renewal keeps customer boxes alive and outranks telemetry."""
    mothership = MagicMock()
    mothership.enabled = True
    mothership.renew_lease = AsyncMock(return_value={"lease_expires_at": "later"})
    mothership.report_rails_health = AsyncMock(side_effect=RuntimeError("boom"))

    manager = _make_manager(mothership, last_activity_seconds_ago=5)
    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(0, 25000))):
        with patch.object(manager, "_sync_instance_lock", MagicMock()):
            await manager._tick()

    assert mothership.renew_lease.await_count == 1, (
        "a failed health report must never stop lease renewal"
    )


@pytest.mark.asyncio
async def test_probe_failure_does_not_break_lease_renewal():
    """Same guarantee, one layer lower: the probe itself blowing up."""
    mothership = MagicMock()
    mothership.enabled = True
    mothership.renew_lease = AsyncMock(return_value={"lease_expires_at": "later"})
    mothership.report_rails_health = AsyncMock(return_value=None)

    manager = _make_manager(mothership, last_activity_seconds_ago=5)
    with patch.object(manager, "_probe_rails", AsyncMock(side_effect=RuntimeError("x"))):
        with patch.object(manager, "_sync_instance_lock", MagicMock()):
            await manager._tick()

    assert mothership.renew_lease.await_count == 1


# --------------------------------------------------------------------------
# Model policy arrives on the SAME authenticated channel (0.7.6)
# --------------------------------------------------------------------------
#
# The mothership needs to move a box's model without SSHing in to edit .env —
# that is what made Meta's 2026-08-31 retirement a fleet outage. It rides the
# lease-renew RESPONSE rather than a new inbound endpoint: pull, not push, so
# there is no new listening surface and no second credential. Same shape as the
# instance-lock backstop already in this loop.

@pytest.mark.asyncio
async def test_a_model_policy_on_the_lease_response_is_stored():
    from app.services import model_policy_store

    mothership = MagicMock()
    mothership.enabled = True
    mothership.report_rails_health = AsyncMock(return_value=None)
    mothership.renew_lease = AsyncMock(
        return_value={"lease_expires_at": "later",
                      "model_policy": {"default_model": "glm-5.3-flash-zai"}}
    )

    manager = _make_manager(mothership, last_activity_seconds_ago=5)
    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(200, 10))):
        with patch.object(manager, "_sync_instance_lock", MagicMock()):
            with patch.object(model_policy_store, "save") as save:
                await manager._tick()

    save.assert_called_once()
    assert save.call_args[0][0]["default_model"] == "glm-5.3-flash-zai"


@pytest.mark.asyncio
async def test_a_lease_response_without_a_policy_changes_nothing():
    """An older mothership sends no such key; that must not wipe the box's policy."""
    from app.services import model_policy_store

    mothership = MagicMock()
    mothership.enabled = True
    mothership.report_rails_health = AsyncMock(return_value=None)
    mothership.renew_lease = AsyncMock(return_value={"lease_expires_at": "later"})

    manager = _make_manager(mothership, last_activity_seconds_ago=5)
    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(200, 10))):
        with patch.object(manager, "_sync_instance_lock", MagicMock()):
            with patch.object(model_policy_store, "save") as save:
                with patch.object(model_policy_store, "clear") as clear:
                    await manager._tick()

    save.assert_not_called()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_a_broken_policy_store_never_breaks_lease_renewal():
    from app.services import model_policy_store

    mothership = MagicMock()
    mothership.enabled = True
    mothership.report_rails_health = AsyncMock(return_value=None)
    mothership.renew_lease = AsyncMock(
        return_value={"lease_expires_at": "later", "model_policy": {"default_model": "x"}}
    )

    manager = _make_manager(mothership, last_activity_seconds_ago=5)
    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(200, 10))):
        with patch.object(manager, "_sync_instance_lock", MagicMock()) as lock:
            with patch.object(model_policy_store, "save", side_effect=OSError("disk")):
                await manager._tick()

    lock.assert_called_once(), "a failed policy write must not stop the rest of the tick"


# --------------------------------------------------------------------------
# Policy must reach an IDLE box too
# --------------------------------------------------------------------------
#
# The lease-renew response only exists when the box renewed its lease, and renewal is
# gated on ACTIVITY_THRESHOLD. So carrying the model policy there alone means an idle
# box never receives it — and during the 2026-08-31 retirement most of the fleet was
# idle at 22:46 UTC. A model change has to reach a box BEFORE its next user arrives,
# not five minutes after.
#
# The health report has no such gate: it runs on every tick. So the policy may ride
# either response, and the health one is what makes it universal.

@pytest.mark.asyncio
async def test_an_idle_box_still_receives_a_model_policy():
    """THE GAP. Idle box: no lease renewal, so the lease response never arrives."""
    from app.services import model_policy_store

    mothership = MagicMock()
    mothership.enabled = True
    mothership.renew_lease = AsyncMock(return_value=None)
    mothership.report_rails_health = AsyncMock(
        return_value={"model_policy": {"default_model": "glm-5.3-flash-zai"}}
    )

    manager = _make_manager(mothership, last_activity_seconds_ago=99999)
    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(200, 10))):
        with patch.object(model_policy_store, "save") as save:
            await manager._tick()

    assert mothership.renew_lease.await_count == 0, "precondition: the box is idle"
    save.assert_called_once()
    assert save.call_args[0][0]["default_model"] == "glm-5.3-flash-zai"


@pytest.mark.asyncio
async def test_a_health_response_without_a_policy_changes_nothing():
    """An older mothership returns 200 with no body, or no such key."""
    from app.services import model_policy_store

    mothership = MagicMock()
    mothership.enabled = True
    mothership.renew_lease = AsyncMock(return_value=None)
    mothership.report_rails_health = AsyncMock(return_value=None)

    manager = _make_manager(mothership, last_activity_seconds_ago=99999)
    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(200, 10))):
        with patch.object(model_policy_store, "save") as save:
            with patch.object(model_policy_store, "clear") as clear:
                await manager._tick()

    save.assert_not_called()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_report_rails_health_returns_the_mothership_body():
    """The client has to hand the response back for any of the above to work."""
    client = _make_client()

    async def fake_post(url, *, json, headers):
        return _mock_response(body={"model_policy": {"default_model": "x"}})

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value.post = fake_post
    with patch("httpx.AsyncClient", return_value=mock_ctx):
        result = await client.report_rails_health(rails_status=200, rails_ms=10)

    assert result == {"model_policy": {"default_model": "x"}}
