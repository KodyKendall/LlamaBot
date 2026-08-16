"""HTTP API for the code editor toggle.

POST /api/vscode/enable and POST /api/vscode/disable write the `enable_vscode`
setting AND start or stop the code-server ("code") container in one call. The
docker side effect lives here rather than in PUT /api/site-settings/{key} so no
other setting write can ever start a container.

Both routes are engineer-or-admin, matching PUT /api/site-settings/{key}.

Run with: pytest app/tests/test_vscode_api.py -v
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


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


def _stored(client):
    from app.models import SiteSetting

    with Session(client._engine) as s:
        setting = s.get(SiteSetting, "enable_vscode")
        return setting.value if setting else None


# --- status ----------------------------------------------------------------

def test_status_reports_disabled_and_stopped_by_default(client):
    with patch("app.services.vscode_service.vscode_running", return_value=False):
        body = client.get("/api/vscode/status").json()

    assert body["enabled"] is False
    assert body["running"] is False


def test_status_reports_the_container_state(client):
    with patch("app.services.vscode_service.vscode_running", return_value=True):
        assert client.get("/api/vscode/status").json()["running"] is True


# --- enable ----------------------------------------------------------------

def test_enable_writes_the_setting_and_starts_the_container(client):
    with patch("app.services.vscode_service.start_vscode",
               return_value={"ok": True, "output": ""}) as start:
        r = client.post("/api/vscode/enable")

    assert r.status_code == 200
    start.assert_called_once()
    assert _stored(client) == "true"
    assert r.json()["enabled"] is True


def test_enable_reports_a_failed_start_without_a_500(client):
    """A missing VSCODE_PASSWORD makes compose refuse. The UI must see why."""
    failed = {"ok": False, "output": "VSCODE_PASSWORD must be set in .env"}
    with patch("app.services.vscode_service.start_vscode", return_value=failed):
        r = client.post("/api/vscode/enable")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "VSCODE_PASSWORD" in body["output"]


def test_a_failed_start_does_not_leave_the_setting_on(client):
    """A visible Code tab pointing at a stopped editor is the failure to avoid."""
    failed = {"ok": False, "output": "boom"}
    with patch("app.services.vscode_service.start_vscode", return_value=failed):
        client.post("/api/vscode/enable")

    assert _stored(client) in (None, "false")


# --- disable ---------------------------------------------------------------

def test_disable_writes_the_setting_and_stops_the_container(client):
    with patch("app.services.vscode_service.start_vscode", return_value={"ok": True, "output": ""}):
        client.post("/api/vscode/enable")

    with patch("app.services.vscode_service.stop_vscode",
               return_value={"ok": True, "output": ""}) as stop:
        r = client.post("/api/vscode/disable")

    stop.assert_called_once()
    assert _stored(client) == "false"
    assert r.json()["enabled"] is False


def test_disable_turns_the_setting_off_even_if_the_stop_fails(client):
    """Hiding the tab is the user's intent. A stuck container must not block it."""
    with patch("app.services.vscode_service.start_vscode", return_value={"ok": True, "output": ""}):
        client.post("/api/vscode/enable")

    with patch("app.services.vscode_service.stop_vscode", return_value={"ok": False, "output": "boom"}):
        r = client.post("/api/vscode/disable")

    assert _stored(client) == "false"
    assert r.json()["ok"] is False


# --- permissions -----------------------------------------------------------

@pytest.mark.parametrize("route", ["/api/vscode/enable", "/api/vscode/disable"])
def test_a_plain_user_cannot_move_the_toggle(route):
    c = _build_client(role="user")
    with patch("app.services.vscode_service.start_vscode") as start, \
         patch("app.services.vscode_service.stop_vscode") as stop:
        r = c.post(route)

    assert r.status_code == 403
    start.assert_not_called()
    stop.assert_not_called()


def test_an_admin_can_move_the_toggle():
    c = _build_client(role="user", is_admin=True)
    with patch("app.services.vscode_service.start_vscode", return_value={"ok": True, "output": ""}):
        assert c.post("/api/vscode/enable").status_code == 200
