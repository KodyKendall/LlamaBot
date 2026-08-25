"""Mothership reports must say WHO sent the message, not just which box.

Before 0.7.4 every report carried ``instance_name`` and nothing else about the
person at the keyboard, so a multi-user box produced telemetry nobody could
attribute. These tests pin the wire contract: a ``user`` object keyed on
``llamapress_user_guid`` (the mothership's own identifier for the account —
mapping happens there, not here), and total silence when identity is unknown.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services import user_context
from app.services.mothership_client import MothershipClient, TELEMETRY_DISABLED_ENV

FAKE_CONFIG = {
    "instance_name": "test-instance",
    "mothership_url": "https://mothership.example.com",
    "mothership_api_token": "tok-test",
}

SAMPLE_USER = {
    "id": 3,
    "username": "kody@llamapress.ai",
    "email": "kody@llamapress.ai",
    "llamapress_user_guid": "guid-abc-123",
    "role": "engineer",
    "is_admin": True,
}


@pytest.fixture(autouse=True)
def _reporting_not_suppressed(monkeypatch):
    """These tests assert the reported payloads, so the suite-wide telemetry kill
    switch (app/tests/conftest.py) has to be off. httpx is patched throughout."""
    monkeypatch.delenv(TELEMETRY_DISABLED_ENV, raising=False)


@pytest.fixture(autouse=True)
def _clean_turn_stamp():
    """A leaked stamp would attribute one test's report to another's user — the
    exact failure mode the ContextVar exists to prevent."""
    token = user_context.set_current(None)
    yield
    user_context.reset(token)


def _make_client() -> MothershipClient:
    client = MothershipClient.__new__(MothershipClient)
    client.config = FAKE_CONFIG
    return client


def _capturing_http(captured: dict):
    async def fake_post(url, *, json, headers):
        captured["url"] = url
        captured["payload"] = json
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {"ok": True}
        resp.raise_for_status = MagicMock()
        return resp

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)
    return mock_http


async def _post_and_capture(coro_factory) -> dict:
    captured = {}
    with patch(
        "app.services.mothership_client.httpx.AsyncClient",
        return_value=_capturing_http(captured),
    ):
        await coro_factory()
    return captured["payload"]


# ---------------------------------------------------------------------------
# describe() — the wire shape
# ---------------------------------------------------------------------------


def test_describe_carries_the_guid_the_mothership_minted():
    """The guid is the join key: the mothership issued it during unified login,
    so it maps guid -> llamapress.ai account with no translation on this side."""
    user = SimpleNamespace(
        id=3,
        username="kody@llamapress.ai",
        email="kody@llamapress.ai",
        llamapress_user_guid="guid-abc-123",
        role="engineer",
        is_admin=True,
    )
    assert user_context.describe(user) == SAMPLE_USER


def test_describe_legacy_user_reports_null_guid_not_a_guess():
    """A username/password user predates unified login. There is no
    llamapress.ai account to point at, and inventing one from the username
    would be a fabricated identity, so the guid stays None."""
    user = SimpleNamespace(
        id=1, username="admin", email=None, llamapress_user_guid=None,
        role="engineer", is_admin=True,
    )
    described = user_context.describe(user)
    assert described["llamapress_user_guid"] is None
    assert described["username"] == "admin"


def test_describe_none_is_none():
    assert user_context.describe(None) is None


def test_from_token_payload_marks_a_rails_gem_caller():
    """A gem token names a RAILS user in a different namespace — reportable as a
    partial identity, but never as a llamapress.ai account."""
    described = user_context.from_token_payload({
        "sub": "rails_user:5",
        "source": "llama_bot_rails",
        "rails_user_id": 5,
        "role": "rails",
        "is_admin": False,
    })
    assert described["username"] == "rails_user:5"
    assert described["rails_user_id"] == 5
    assert described["llamapress_user_guid"] is None
    assert described["id"] is None


# ---------------------------------------------------------------------------
# report_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_message_includes_explicit_user():
    client = _make_client()
    payload = await _post_and_capture(lambda: client.report_message(
        thread_id="t-1", role="user", content="hi",
        sent_at="2026-08-25T00:00:00+00:00", user=SAMPLE_USER,
    ))
    assert payload["user"] == SAMPLE_USER


@pytest.mark.asyncio
async def test_report_message_falls_back_to_the_turn_stamp():
    """The WebSocket path cannot pass a user: report_message fires inside a
    RequestHandler built before authentication. It reads the stamp instead."""
    client = _make_client()
    user_context.set_current(SAMPLE_USER)
    payload = await _post_and_capture(lambda: client.report_message(
        thread_id="t-1", role="assistant", content="hello",
        sent_at="2026-08-25T00:00:00+00:00",
    ))
    assert payload["user"] == SAMPLE_USER


@pytest.mark.asyncio
async def test_report_message_omits_user_when_unknown():
    """No stamp, no argument -> the payload looks exactly as it did before this
    feature. An unattributed row beats a wrong one."""
    client = _make_client()
    payload = await _post_and_capture(lambda: client.report_message(
        thread_id="t-1", role="user", content="hi",
        sent_at="2026-08-25T00:00:00+00:00",
    ))
    assert "user" not in payload


@pytest.mark.asyncio
async def test_explicit_user_wins_over_the_stamp():
    client = _make_client()
    user_context.set_current(SAMPLE_USER)
    other = dict(SAMPLE_USER, id=9, llamapress_user_guid="guid-other")
    payload = await _post_and_capture(lambda: client.report_message(
        thread_id="t-1", role="user", content="hi",
        sent_at="2026-08-25T00:00:00+00:00", user=other,
    ))
    assert payload["user"]["llamapress_user_guid"] == "guid-other"


# ---------------------------------------------------------------------------
# the other three report paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_error_includes_user():
    client = _make_client()
    user_context.set_current(SAMPLE_USER)
    payload = await _post_and_capture(lambda: client.report_error(
        thread_id="t-1", error_class="ValueError",
        error_message="boom", traceback_str="tb",
    ))
    assert payload["user"] == SAMPLE_USER


@pytest.mark.asyncio
async def test_report_turn_metrics_includes_user():
    client = _make_client()
    user_context.set_current(SAMPLE_USER)
    payload = await _post_and_capture(lambda: client.report_turn_metrics(
        thread_id="t-1", metrics={"total_ms": 1200},
    ))
    assert payload["user"] == SAMPLE_USER


@pytest.mark.asyncio
async def test_submit_feedback_includes_user():
    client = _make_client()
    payload = await _post_and_capture(lambda: client.submit_feedback(
        thread_id="t-1", rating="good", user=SAMPLE_USER,
    ))
    assert payload["user"] == SAMPLE_USER


# ---------------------------------------------------------------------------
# attribution safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_turns_do_not_cross_attribute():
    """Two users' turns run in the same process. A global would hand one
    person's message to the other's account; a ContextVar is per-task."""
    client = _make_client()
    alice = dict(SAMPLE_USER, id=1, username="alice", llamapress_user_guid="guid-alice")
    bob = dict(SAMPLE_USER, id=2, username="bob", llamapress_user_guid="guid-bob")
    seen = {}

    async def turn(who, delay):
        user_context.set_current(who)
        await asyncio.sleep(delay)
        payload = await _post_and_capture(lambda: client.report_message(
            thread_id=f"t-{who['username']}", role="user", content="hi",
            sent_at="2026-08-25T00:00:00+00:00",
        ))
        seen[who["username"]] = payload["user"]["llamapress_user_guid"]

    await asyncio.gather(turn(alice, 0.02), turn(bob, 0.0))

    assert seen == {"alice": "guid-alice", "bob": "guid-bob"}


@pytest.mark.asyncio
async def test_telemetry_kill_switch_still_wins(monkeypatch):
    """Adding identity must not sneak a report past the harness kill switch."""
    monkeypatch.setenv(TELEMETRY_DISABLED_ENV, "1")
    client = _make_client()
    user_context.set_current(SAMPLE_USER)
    mock_http = _capturing_http({})
    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
        result = await client.report_message(
            thread_id="t-1", role="user", content="hi",
            sent_at="2026-08-25T00:00:00+00:00",
        )
    assert result is None
    mock_http.post.assert_not_called()


def test_resolve_user_never_raises_when_context_import_fails():
    """Telemetry identity is best-effort; a broken lookup must not break a turn."""
    with patch("app.services.user_context.current", side_effect=RuntimeError("boom")):
        assert MothershipClient._resolve_user(None) is None
