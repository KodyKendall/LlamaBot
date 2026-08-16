"""The remote "your free Leo is about to sleep" lock.

Covers the three things that make it a lock rather than a suggestion:

  * the state round-trips through the auth DB (so it survives a restart) and
    fails OPEN when that DB is unreadable — a down database must never lock a
    paying user out of their own Leo;
  * only the mothership can set it (the instance owner is an admin on their own
    box, so an admin-gated write would let them unlock themselves);
  * the modal is genuinely non-dismissible and the composer is disabled behind
    it, and the WebSocket refuses turns independently of the modal.
"""

import json
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import SiteSetting  # noqa: F401 — registers the table
from app.services import instance_lock

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
CHAT_HTML = (FRONTEND / "chat.html").read_text()
STYLE_CSS = (FRONTEND / "style.css").read_text()
MESSAGE_HANDLER_JS = (FRONTEND / "chat" / "websocket" / "MessageHandler.js").read_text()
API_PY = (Path(__file__).resolve().parents[1] / "routers" / "api.py").read_text()
UI_PY = (Path(__file__).resolve().parents[1] / "routers" / "ui.py").read_text()
REQUEST_HANDLER_PY = (
    Path(__file__).resolve().parents[1] / "websocket" / "request_handler.py"
).read_text()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ---------------------------------------------------------------- state layer


def test_default_state_is_unlocked(session):
    state = instance_lock.get_lock_state(session)
    assert state["locked"] is False
    assert state["upgrade_url"] == "https://llamapress.ai/pricing"


def test_lock_state_round_trips_through_the_database(session):
    instance_lock.set_lock_state(session, {"locked": True})
    assert instance_lock.is_locked(session) is True

    # A brand-new read (as after a container restart) still sees the lock.
    row = session.get(SiteSetting, instance_lock.SETTING_KEY)
    assert json.loads(row.value)["locked"] is True


def test_unlock_clears_the_lock(session):
    instance_lock.set_lock_state(session, {"locked": True})
    instance_lock.set_lock_state(session, {"locked": False})
    assert instance_lock.is_locked(session) is False


def test_copy_can_be_overridden_by_the_mothership(session):
    state = instance_lock.set_lock_state(
        session,
        {"locked": True, "title": "Time's up", "upgrade_url": "https://example.com/x"},
    )
    assert state["title"] == "Time's up"
    assert state["upgrade_url"] == "https://example.com/x"
    # Unspecified fields keep the shipped default rather than going blank.
    assert state["body"] == instance_lock.DEFAULT_BODY


def test_oversized_copy_is_rejected_rather_than_truncated(session):
    with pytest.raises(ValueError):
        instance_lock.set_lock_state(session, {"locked": True, "body": "x" * 1200})


def test_malformed_row_reads_as_unlocked(session):
    session.add(SiteSetting(key=instance_lock.SETTING_KEY, value="not json"))
    session.commit()
    assert instance_lock.is_locked(session) is False


def test_unreadable_database_fails_open():
    class BrokenSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("auth DB is down")

    assert instance_lock.is_locked(BrokenSession()) is False


# ------------------------------------------------------------------- auth


def _request(headers):
    class FakeApp:
        class state:
            mothership_client = None

    class FakeRequest:
        def __init__(self, headers):
            self.headers = headers
            self.app = FakeApp

    return FakeRequest(headers)


def test_only_the_mothership_token_authorizes_a_lock(monkeypatch):
    from app.routers import api

    class FakeClient:
        config = {"mothership_api_token": "secret-token"}

    monkeypatch.setattr("app.services.mothership_client.MothershipClient", lambda: FakeClient())

    assert api._mothership_authorized(_request({"authorization": "Bearer secret-token"})) is True
    assert api._mothership_authorized(_request({"authorization": "Bearer wrong"})) is False
    assert api._mothership_authorized(_request({"authorization": "secret-token"})) is False
    assert api._mothership_authorized(_request({})) is False


def test_unconfigured_instance_authorizes_nobody(monkeypatch):
    from app.routers import api

    class NoConfig:
        config = None

    monkeypatch.setattr("app.services.mothership_client.MothershipClient", lambda: NoConfig())
    assert api._mothership_authorized(_request({"authorization": "Bearer anything"})) is False


def test_lock_key_is_not_a_user_settable_site_setting():
    # In VALID_SITE_SETTINGS the PUT is engineer-or-admin — i.e. the instance
    # owner could unlock their own box with one curl.
    from app.routers.api import VALID_SITE_SETTINGS

    assert instance_lock.SETTING_KEY not in VALID_SITE_SETTINGS


# --------------------------------------------------------------- wiring


def test_api_exposes_a_read_endpoint_and_a_mothership_only_write():
    assert '@router.get("/api/instance-lock"' in API_PY
    assert '@router.post("/api/instance-lock"' in API_PY
    assert "_mothership_authorized(request)" in API_PY


def test_lock_state_is_injected_into_the_page():
    # Without this a locked instance would paint a usable UI until the first poll.
    assert "window.LLAMABOT_INSTANCE_LOCK = " in UI_PY


def test_websocket_refuses_turns_while_locked():
    assert "await self._check_instance_lock_or_block(websocket)" in REQUEST_HANDLER_PY
    # Must run before the paywall gate so a blocked turn never burns quota.
    lock_at = REQUEST_HANDLER_PY.index("await self._check_instance_lock_or_block(websocket)")
    paywall_at = REQUEST_HANDLER_PY.index("await self._check_paywall_or_block(websocket)")
    assert lock_at < paywall_at


# ---------------------------------------------------------------- frontend


def _lock_modal_markup() -> str:
    start = CHAT_HTML.index('data-llamabot="lock-modal"')
    return CHAT_HTML[start : CHAT_HTML.index("<script>", start)]


def test_modal_has_no_dismiss_affordance():
    markup = _lock_modal_markup()
    assert 'data-llamabot="lock-modal-cta"' in markup
    for escape_hatch in ("lock-modal-close", "lock-modal-cancel", "lock-modal-dismiss"):
        assert escape_hatch not in markup


def test_escape_does_not_close_the_lock():
    assert "if (locked && e.key === 'Escape')" in CHAT_HTML


def test_modal_polls_so_a_lock_lands_without_a_refresh():
    assert "fetch('/api/instance-lock'" in CHAT_HTML
    assert "setInterval(poll, POLL_MS)" in CHAT_HTML


def test_composer_is_disabled_behind_the_lock():
    assert "setComposerEnabled(false)" in CHAT_HTML
    assert "body.instance-locked .chat-section" in STYLE_CSS


def test_lock_overlay_sits_above_the_other_modals():
    lock_z = STYLE_CSS.index(".lock-modal-overlay {")
    assert "z-index: 10050" in STYLE_CSS[lock_z : lock_z + 300]


def test_websocket_frame_locks_instantly():
    assert "data.type === 'instance_locked'" in MESSAGE_HANDLER_JS
    assert "window.__llamabotApplyInstanceLock" in MESSAGE_HANDLER_JS
    assert "window.__llamabotApplyInstanceLock = apply" in CHAT_HTML
