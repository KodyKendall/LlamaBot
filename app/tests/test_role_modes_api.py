"""Admin API for role → agent-mode grants (GET/PUT /api/role-modes).

Deliberately admin-only, unlike /api/site-settings/{key} which is
engineer-or-admin — an engineer editing role grants could widen another role.

Run with: pytest app/tests/test_role_modes_api.py -v
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.permissions import ROLE_MODES_SETTING_KEY


@pytest.fixture
def client():
    import app.models  # noqa: F401
    from app.dependencies import admin_required, get_db_session
    from app.models import User
    from app.routers.api import router

    # StaticPool + check_same_thread: TestClient runs the app on another thread,
    # and a default in-memory SQLite engine would hand it a fresh, empty DB.
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
    app.dependency_overrides[admin_required] = lambda: User(
        username="admin", password_hash="x", is_admin=True, role="engineer"
    )

    c = TestClient(app)
    c._engine = engine
    return c


def _stored(client):
    from app.models import SiteSetting

    with Session(client._engine) as s:
        setting = s.get(SiteSetting, ROLE_MODES_SETTING_KEY)
        return json.loads(setting.value) if setting else None


# --- GET -------------------------------------------------------------------

def test_get_returns_defaults_and_the_mode_catalog(client):
    r = client.get("/api/role-modes")
    assert r.status_code == 200
    body = r.json()
    assert body["role_modes"]["user"] == ["chat"]
    assert "engineer" in body["role_modes"]
    assert "pyxl" in body["all_modes"] and "chat" in body["all_modes"]
    assert body["defaults"]["user"] == ["chat"]


def test_get_reflects_a_saved_config(client):
    client.put("/api/role-modes", json={"role_modes": {"user": ["chat", "beginner"]}})
    assert client.get("/api/role-modes").json()["role_modes"]["user"] == ["chat", "beginner"]


# --- PUT -------------------------------------------------------------------

def test_put_persists_the_grant(client):
    r = client.put("/api/role-modes", json={"role_modes": {"user": ["chat", "beginner"]}})
    assert r.status_code == 200
    assert _stored(client) == {"user": ["chat", "beginner"]}


def test_put_preserves_admin_ordering(client):
    """Order drives the chat dropdown, so it must survive the round trip."""
    client.put("/api/role-modes", json={"role_modes": {"user": ["pyxl", "chat"]}})
    assert _stored(client)["user"] == ["pyxl", "chat"]


def test_put_dedupes(client):
    client.put("/api/role-modes", json={"role_modes": {"user": ["chat", "chat"]}})
    assert _stored(client)["user"] == ["chat"]


def test_put_accepts_an_empty_grant(client):
    """Revoking everything from a role is legitimate — admins keep access regardless."""
    r = client.put("/api/role-modes", json={"role_modes": {"user": []}})
    assert r.status_code == 200
    assert _stored(client) == {"user": []}


def test_put_replaces_rather_than_merges(client):
    client.put("/api/role-modes", json={"role_modes": {"user": ["chat"], "engineer": ["ticket"]}})
    client.put("/api/role-modes", json={"role_modes": {"user": ["beginner"]}})
    assert _stored(client) == {"user": ["beginner"]}


# --- validation: a typo must fail loudly, not silently narrow access -------

def test_put_rejects_an_unknown_mode(client):
    r = client.put("/api/role-modes", json={"role_modes": {"user": ["chat", "typo_mode"]}})
    assert r.status_code == 400
    assert "typo_mode" in r.json()["detail"]
    assert _stored(client) is None


@pytest.mark.parametrize("payload", [
    {},
    {"role_modes": "nope"},
    {"role_modes": {"user": "chat"}},
    {"role_modes": {"user": [1, 2]}},
    {"role_modes": {"": ["chat"]}},
])
def test_put_rejects_malformed_payloads(client, payload):
    assert client.put("/api/role-modes", json=payload).status_code == 400


def test_put_rejects_a_config_too_large_to_store(client):
    """SiteSetting.value is max_length=1000 — reject rather than truncate."""
    r = client.put("/api/role-modes", json={
        "role_modes": {f"role_{i}": ["chat", "engineer", "pyxl"] for i in range(60)}
    })
    assert r.status_code == 400
    assert _stored(client) is None


# --- authorization ---------------------------------------------------------

def test_endpoints_require_admin():
    """The dependency itself — a non-admin must not reach either handler."""
    from app.dependencies import admin_required, get_current_user
    from app.models import User
    from fastapi import HTTPException

    engineer = User(username="eng", password_hash="x", is_admin=False, role="engineer")
    with pytest.raises(HTTPException) as exc:
        admin_required(current_user=engineer)
    assert exc.value.status_code == 403
