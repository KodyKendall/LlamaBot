"""Voice dictation: the AssemblyAI key gate, the Settings toggle, and the token route.

The browser streams audio straight to AssemblyAI, so it needs a credential — but
never the master key. POST /api/voice-dictation/token trades the server-held key
for a 60-second streaming token and returns only that token.

Since 0.7.16 (LP-18) a box that never saved `enable_voice_dictation` gets the mic
button — except a ZDR/FERPA box, where the unsaved default stays off. An explicit
save of "false" always wins. Nothing works without an AssemblyAI key (the Settings
toggle is greyed out, and the PUT refuses "true" server-side too).

Run with: pytest app/tests/test_voice_dictation.py -v
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

MASTER_KEY = "aai-master-key-must-never-reach-the-browser"
KEY_NAME = "ASSEMBLY_AI_API_KEY"


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    """Every test starts on a box with no AssemblyAI key, whatever the container has."""
    for name in (KEY_NAME, "ASSEMBLYAI_API_KEY", "ASSEMBLY_AI_API"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _not_zdr(monkeypatch):
    """Every test starts on a normal (non-ZDR) box, whatever policy the container holds."""
    from app.agents.leonardo import zdr

    monkeypatch.setattr(zdr, "enforced", lambda: False)


@pytest.fixture
def zdr_box(monkeypatch):
    from app.agents.leonardo import zdr

    monkeypatch.setattr(zdr, "enforced", lambda: True)


def _build_client(role="engineer", is_admin=False):
    import app.models  # noqa: F401
    from app.dependencies import auth, get_current_user, get_db_session
    from app.models import User
    from app.routers.api import router

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[auth] = lambda: "dev"
    app.dependency_overrides[get_current_user] = lambda: User(
        username="dev", password_hash="x", is_admin=is_admin, role=role
    )

    c = TestClient(app)
    c._engine = engine
    return c


@pytest.fixture
def client():
    return _build_client()


def _save(client, value):
    from app.models import SiteSetting

    with Session(client._engine) as s:
        s.add(SiteSetting(key="enable_voice_dictation", value=value))
        s.commit()


def _enable(client):
    _save(client, "true")


def _upstream(status=200, body=None):
    """A fake httpx.AsyncClient whose GET returns one canned AssemblyAI response."""
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body if body is not None else {
        "token": "temp-streaming-token", "expires_in_seconds": 60,
    }
    http = MagicMock()
    http.get = AsyncMock(return_value=response)
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    return http


# --- the key ---------------------------------------------------------------

def test_key_is_read_from_assembly_ai_api_key(monkeypatch):
    from app.services import voice_dictation

    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    assert voice_dictation.api_key() == MASTER_KEY
    assert voice_dictation.key_configured() is True


@pytest.mark.parametrize("other_spelling", ["ASSEMBLYAI_API_KEY", "ASSEMBLY_AI_API"])
def test_other_spellings_are_not_read(monkeypatch, other_spelling):
    from app.services import voice_dictation

    monkeypatch.setenv(other_spelling, MASTER_KEY)
    assert voice_dictation.key_configured() is False


def test_blank_key_counts_as_missing(monkeypatch):
    from app.services import voice_dictation

    monkeypatch.setenv(KEY_NAME, "   ")
    assert voice_dictation.key_configured() is False


# --- status ----------------------------------------------------------------

def test_status_on_by_default_with_a_key(client, monkeypatch):
    """LP-18 scenario 1: key present, setting never saved → mic button."""
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    body = client.get("/api/voice-dictation/status").json()

    assert body == {"key_configured": True, "enabled": True, "available": True}


def test_status_explicit_false_wins_over_the_default(client, monkeypatch):
    """LP-18 scenario 2: an admin who switched it off keeps it off."""
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    _save(client, "false")
    body = client.get("/api/voice-dictation/status").json()

    assert body["enabled"] is False
    assert body["available"] is False


def test_status_off_by_default_without_a_key(client):
    """LP-18 scenario 3: no key, setting never saved → no mic button."""
    body = client.get("/api/voice-dictation/status").json()

    assert body["key_configured"] is False
    assert body["available"] is False


def test_status_off_by_default_on_a_zdr_box(client, monkeypatch, zdr_box):
    """LP-18 scenario 4: FERPA/ZDR box with a key and no saved setting stays off."""
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    body = client.get("/api/voice-dictation/status").json()

    assert body == {"key_configured": True, "enabled": False, "available": False}


def test_token_refused_by_default_on_a_zdr_box(client, monkeypatch, zdr_box):
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    assert client.post("/api/voice-dictation/token").status_code == 403


def test_unreadable_setting_fails_closed(monkeypatch):
    from app.services import voice_dictation

    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    with patch("app.routers.api.get_site_setting", side_effect=RuntimeError("db down")):
        assert voice_dictation.dictation_available(MagicMock()) is False


def test_status_reports_a_missing_key(client):
    _enable(client)
    body = client.get("/api/voice-dictation/status").json()

    assert body["key_configured"] is False
    assert body["available"] is False


def test_status_available_with_key_and_toggle_on(client, monkeypatch):
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    _enable(client)
    assert client.get("/api/voice-dictation/status").json()["available"] is True


def test_status_never_contains_the_key(client, monkeypatch):
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    _enable(client)
    assert MASTER_KEY not in client.get("/api/voice-dictation/status").text


# --- the setting -----------------------------------------------------------

def test_setting_is_a_known_site_setting():
    from app.routers.api import VALID_SITE_SETTINGS

    assert "enable_voice_dictation" in VALID_SITE_SETTINGS


def test_setting_cannot_be_switched_on_without_a_key(client):
    r = client.put("/api/site-settings/enable_voice_dictation", json={"value": "true"})

    assert r.status_code == 400
    assert client.get("/api/voice-dictation/status").json()["available"] is False


def test_setting_can_always_be_switched_off(client):
    r = client.put("/api/site-settings/enable_voice_dictation", json={"value": "false"})
    assert r.status_code == 200


def test_setting_switches_on_with_a_key(client, monkeypatch):
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    r = client.put("/api/site-settings/enable_voice_dictation", json={"value": "true"})

    assert r.status_code == 200
    assert client.get("/api/voice-dictation/status").json()["available"] is True


# --- the token -------------------------------------------------------------

def test_token_returns_only_the_temporary_token(client, monkeypatch):
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    _enable(client)
    http = _upstream()

    with patch("app.services.voice_dictation.httpx.AsyncClient", return_value=http):
        r = client.post("/api/voice-dictation/token")

    assert r.status_code == 200
    assert r.json() == {"token": "temp-streaming-token", "expires_in_seconds": 60}
    assert MASTER_KEY not in r.text
    assert "no-store" in r.headers.get("cache-control", "")


def test_token_request_uses_the_v3_endpoint_with_a_60s_lifetime(client, monkeypatch):
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    _enable(client)
    http = _upstream()

    with patch("app.services.voice_dictation.httpx.AsyncClient", return_value=http):
        client.post("/api/voice-dictation/token")

    args, kwargs = http.get.call_args
    assert args[0] == "https://streaming.assemblyai.com/v3/token"
    assert kwargs["params"] == {"expires_in_seconds": 60}
    # The master key travels server → AssemblyAI only, in the header.
    assert kwargs["headers"]["Authorization"] == MASTER_KEY


def test_token_refused_while_the_toggle_is_off(client, monkeypatch):
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    _save(client, "false")
    http = _upstream()

    with patch("app.services.voice_dictation.httpx.AsyncClient", return_value=http):
        r = client.post("/api/voice-dictation/token")

    assert r.status_code == 403
    http.get.assert_not_called()


def test_token_refused_without_a_key_even_if_the_setting_is_on(client):
    _enable(client)
    r = client.post("/api/voice-dictation/token")
    assert r.status_code == 403


def test_upstream_failure_is_a_502_without_leaking_anything(client, monkeypatch):
    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    _enable(client)
    http = _upstream(status=401, body={"error": f"bad key {MASTER_KEY}"})

    with patch("app.services.voice_dictation.httpx.AsyncClient", return_value=http):
        r = client.post("/api/voice-dictation/token")

    assert r.status_code == 502
    assert MASTER_KEY not in r.text


def test_upstream_unreachable_is_a_502(client, monkeypatch):
    import httpx

    monkeypatch.setenv(KEY_NAME, MASTER_KEY)
    _enable(client)
    http = _upstream()
    http.get = AsyncMock(side_effect=httpx.ConnectError("down"))

    with patch("app.services.voice_dictation.httpx.AsyncClient", return_value=http):
        assert client.post("/api/voice-dictation/token").status_code == 502


# --- the pages -------------------------------------------------------------

def test_chat_flag_true_only_with_key_and_toggle(monkeypatch):
    from app.services import voice_dictation

    session = MagicMock()
    with patch("app.routers.api.get_site_setting", return_value="true"):
        assert voice_dictation.dictation_available(session) is False
        monkeypatch.setenv(KEY_NAME, MASTER_KEY)
        assert voice_dictation.dictation_available(session) is True
    with patch("app.routers.api.get_site_setting", return_value="false"):
        assert voice_dictation.dictation_available(session) is False


def test_chat_page_injects_the_flag():
    from pathlib import Path

    ui = (Path(__file__).resolve().parents[1] / "routers" / "ui.py").read_text()
    assert "window.LLAMABOT_VOICE_DICTATION" in ui


def test_settings_toggle_greyed_out_without_a_key():
    from app.routers.ui import voice_dictation_setting_row

    row = voice_dictation_setting_row(enabled=False, key_configured=False)
    assert 'id="voiceDictationToggle"' in row
    assert "disabled" in row
    assert "checked" not in row
    assert KEY_NAME in row   # tells the admin what is missing


def test_settings_toggle_live_with_a_key():
    from app.routers.ui import voice_dictation_setting_row

    off = voice_dictation_setting_row(enabled=False, key_configured=True)
    on = voice_dictation_setting_row(enabled=True, key_configured=True)
    assert "disabled" not in off and "checked" not in off
    assert "checked" in on
