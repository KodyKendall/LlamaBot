"""Security admin vs system admin vs standard user (LP-32, HECVAT APPL-05).

These go through the real login dependency, so the route boundary is what is tested.
``is_admin`` stays as the account owner, who holds both.
"""
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import permissions
from app.agents.leonardo import tool_limits, zdr
from app.lib import request_context
from app.models import AuditEvent, User
from app.services.user_service import hash_password

PASSWORD = "correct-horse"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _user(db, username, **flags):
    user = User(username=username, password_hash=hash_password(PASSWORD), role=flags.pop("role", "engineer"), **flags)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def people(db):
    return {
        "owner": _user(db, "owner", is_admin=True),
        "security": _user(db, "security", is_security_admin=True),
        "system": _user(db, "system", is_system_admin=True),
        "standard": _user(db, "standard"),
    }


# Only these run their real handlers, against the in-memory database above. Everything
# else keeps its real auth dependencies but gets a stub handler: this container has the
# live repos mounted, so a real /api/git/push or /api/checkpoints/discard would really
# push or really throw away uncommitted work if a guard ever failed.
REAL_HANDLERS = ("/api/users", "/api/audit-events")


def _stub():
    return {"stubbed": True}


@pytest.fixture
def client(db, monkeypatch):
    from fastapi import Depends
    from fastapi.routing import APIRoute
    from app.dependencies import get_db_session
    from app.routers import api, checkpoints, scheduled_jobs, slash_commands, ui

    monkeypatch.setattr(zdr, "enforced", lambda: False)
    app = FastAPI()
    for module in (api, checkpoints, scheduled_jobs, slash_commands, ui):
        for route in module.router.routes:
            if not isinstance(route, APIRoute):
                continue
            if route.path.startswith(REAL_HANDLERS):
                # Re-registered so that this app's database override reaches it.
                app.add_api_route(route.path, route.endpoint, methods=list(route.methods))
                continue
            guards = [Depends(d.call) for d in route.dependant.dependencies]
            app.add_api_route(route.path, _stub, methods=list(route.methods), dependencies=guards)
    app.dependency_overrides[get_db_session] = lambda: db
    return TestClient(app, raise_server_exceptions=False)


def _as(username):
    token = base64.b64encode(f"{username}:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _events(db, kind=None):
    rows = db.exec(select(AuditEvent)).all()
    return [row for row in rows if kind is None or row.event_type == kind]


SECURITY_ROUTES = [
    ("get", "/api/users", None),
    ("post", "/api/users", {"username": "new", "password": PASSWORD}),
    ("put", "/api/role-write-tools", {"grants": {"user": False}}),
    ("put", "/api/sensitive-data-rules", {}),
    ("get", "/api/audit-events", None),
    ("get", "/users", None),
]

SYSTEM_ROUTES = [
    ("post", "/api/update", {}),
    ("post", "/api/env-vars/apply", {}),
    ("post", "/api/custom-env-vars", {"name": "X", "value": "1"}),
    ("put", "/api/leonardo-md", {"content": "x"}),
    ("put", "/api/role-modes", {"role_modes": {"user": ["chat"]}}),
    ("put", "/api/site-settings/some_key", {"value": "x"}),
    ("post", "/api/vscode/enable", {}),
    ("post", "/api/git/push", {}),
    ("post", "/api/checkpoints/discard", {}),
    ("post", "/api/scheduled-jobs", {}),
    ("post", "/api/slash-commands/execute", {}),
    ("get", "/api/env-vars", None),
]


# ---------------------------------------------------------------------------
# The ticket's four proofs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", SYSTEM_ROUTES)
def test_security_admin_cannot_touch_system_settings(client, people, method, path, body):
    response = client.request(method, path, json=body, headers=_as("security"))
    assert response.status_code == 403, response.text


def test_a_refused_attempt_is_audited(client, people, db):
    client.post("/api/update", json={}, headers=_as("security"))
    denied = _events(db, "access_denied")
    assert denied and denied[-1].actor == "security" and "/api/update" in denied[-1].detail


@pytest.mark.parametrize("method,path,body", SECURITY_ROUTES)
def test_system_admin_cannot_touch_users_roles_or_security(client, people, method, path, body):
    response = client.request(method, path, json=body, headers=_as("system"))
    assert response.status_code == 403, response.text


def test_system_admin_cannot_change_a_role_or_reset_a_password(client, people, db):
    target = people["standard"].id
    for body in ({"is_security_admin": True}, {"new_password": "x" * 12}, {"role": "user"}):
        assert client.patch(f"/api/users/{target}", json=body, headers=_as("system")).status_code == 403
    db.refresh(people["standard"])
    assert people["standard"].is_security_admin is False


@pytest.mark.parametrize("method,path,body", SECURITY_ROUTES + SYSTEM_ROUTES[:2])
def test_standard_user_can_do_neither(client, people, method, path, body, monkeypatch):
    # An engineer keeps engineer-level routes on a normal box, so check those under ZDR.
    monkeypatch.setattr(zdr, "enforced", lambda: True)
    response = client.request(method, path, json=body, headers=_as("standard"))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("method,path,body", SYSTEM_ROUTES)
def test_standard_user_on_zdr_cannot_touch_system_settings(client, people, method, path, body, monkeypatch):
    monkeypatch.setattr(zdr, "enforced", lambda: True)
    response = client.request(method, path, json=body, headers=_as("standard"))
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("method,path,body", SYSTEM_ROUTES)
def test_system_admin_gets_past_the_boundary_on_system_routes(client, people, method, path, body, monkeypatch):
    monkeypatch.setattr(zdr, "enforced", lambda: True)
    response = client.request(method, path, json=body, headers=_as("system"))
    assert response.status_code not in (401, 403), response.text


@pytest.mark.parametrize("method,path,body", SECURITY_ROUTES)
def test_security_admin_can_do_security_work(client, people, method, path, body):
    assert client.request(method, path, json=body, headers=_as("security")).status_code < 400


# ---------------------------------------------------------------------------
# Granting roles
# ---------------------------------------------------------------------------

def test_security_admin_can_grant_one_admin_role(client, people, db):
    target = people["standard"].id
    response = client.patch(f"/api/users/{target}", json={"is_system_admin": True}, headers=_as("security"))
    assert response.status_code == 200, response.text
    db.refresh(people["standard"])
    assert people["standard"].is_system_admin is True


def test_nobody_but_an_owner_grants_both(client, people, db):
    target = people["standard"].id
    body = {"is_security_admin": True, "is_system_admin": True, "dual_admin_reason": "small team"}
    assert client.patch(f"/api/users/{target}", json=body, headers=_as("security")).status_code == 403


def test_adding_the_second_role_counts_as_both(client, people, db):
    target = people["system"].id
    body = {"is_security_admin": True, "dual_admin_reason": "small team"}
    assert client.patch(f"/api/users/{target}", json=body, headers=_as("security")).status_code == 403


def test_owner_grants_both_only_with_a_logged_reason(client, people, db):
    target = people["standard"].id
    both = {"is_security_admin": True, "is_system_admin": True}
    assert client.patch(f"/api/users/{target}", json=both, headers=_as("owner")).status_code == 400

    body = {**both, "dual_admin_reason": "two-person IT team"}
    assert client.patch(f"/api/users/{target}", json=body, headers=_as("owner")).status_code == 200
    db.refresh(people["standard"])
    assert people["standard"].is_security_admin and people["standard"].is_system_admin

    event = _events(db, "role_changed")[-1]
    assert event.actor == "owner" and event.subject == "standard"
    assert "two-person IT team" in event.detail and event.created_at


def test_an_admin_cannot_raise_their_own_role(client, people):
    own = people["security"].id
    response = client.patch(f"/api/users/{own}", json={"is_system_admin": True}, headers=_as("security"))
    assert response.status_code == 403


@pytest.mark.parametrize("body", [{"is_admin": True}, {"is_security_admin": True}])
def test_security_admin_cannot_mint_an_owner(client, people, body):
    if "is_admin" in body:
        target = people["standard"].id
        assert client.patch(f"/api/users/{target}", json=body, headers=_as("security")).status_code == 403
    new = {"username": "sneaky", "password": PASSWORD, "is_admin": True}
    assert client.post("/api/users", json=new, headers=_as("security")).status_code == 403


def test_security_admin_cannot_change_an_owner(client, people):
    target = people["owner"].id
    for body in ({"is_active": False}, {"new_password": "x" * 12}, {"is_admin": False}):
        assert client.patch(f"/api/users/{target}", json=body, headers=_as("security")).status_code == 403
    assert client.delete(f"/api/users/{target}", headers=_as("security")).status_code == 403


def test_security_admin_cannot_take_over_a_system_admin_by_password(client, people):
    """Setting their password would hand over their access, so that one is the owner's."""
    target = people["system"].id
    response = client.patch(f"/api/users/{target}", json={"new_password": "x" * 12}, headers=_as("security"))
    assert response.status_code == 403


def test_security_admin_can_revoke_a_system_admin(client, people, db):
    target = people["system"].id
    response = client.patch(f"/api/users/{target}", json={"is_system_admin": False}, headers=_as("security"))
    assert response.status_code == 200
    db.refresh(people["system"])
    assert people["system"].is_system_admin is False


def test_every_role_change_is_logged(client, people, db):
    client.patch(f"/api/users/{people['standard'].id}", json={"is_system_admin": True}, headers=_as("security"))
    event = _events(db, "role_changed")[-1]
    assert event.actor == "security" and event.subject == "standard" and "is_system_admin" in event.detail


def test_audit_events_cannot_be_edited_or_deleted(client, people, db):
    client.post("/api/update", json={}, headers=_as("security"))
    event = _events(db)[-1]
    event.detail = "nothing to see"
    with pytest.raises(Exception):
        db.commit()
    db.rollback()
    with pytest.raises(Exception):
        db.delete(_events(db)[-1])
        db.commit()
    db.rollback()


# ---------------------------------------------------------------------------
# Agent modes and tools
# ---------------------------------------------------------------------------

def test_system_admin_gets_every_mode_and_security_admin_does_not(db, people):
    everything = permissions.allowed_modes(db, "user", True)
    assert permissions.allowed_modes(db, "user", permissions.holds_system_admin(people["system"])) == everything
    assert permissions.holds_system_admin(people["security"]) is False


def test_new_token_claims_carry_the_roles(people):
    from app.services.token_service import create_ws_token, verify_ws_token

    claims = verify_ws_token(create_ws_token(people["security"]))
    assert claims["is_security_admin"] is True and claims["is_system_admin"] is False


@pytest.mark.parametrize("on_zdr", [True, False])
def test_security_admin_tools_are_read_only_on_any_box(monkeypatch, on_zdr):
    from langchain_core.tools import tool

    @tool
    def write_file(path: str) -> str:
        """Write."""
        return "written"

    @tool
    def read_file(path: str) -> str:
        """Read."""
        return "contents"

    monkeypatch.setattr(zdr, "enforced", lambda: on_zdr)
    monkeypatch.setattr(permissions, "role_write_tool_grants", lambda: {"engineer": True})
    permissions.invalidate_tool_grants()
    token = request_context.set_current_principal("engineer", False, security_only=True)
    try:
        assert [t.name for t in tool_limits.visible_tools([read_file, write_file])] == ["read_file"]
        with pytest.raises(tool_limits.TurnSafetyStop):
            write_file.invoke({"path": "x"})
    finally:
        request_context.reset_current_principal(token)
        permissions.invalidate_tool_grants()


def test_system_admin_keeps_write_tools_on_zdr(monkeypatch):
    monkeypatch.setattr(zdr, "enforced", lambda: True)
    monkeypatch.setattr(permissions, "role_write_tool_grants", lambda: {})
    permissions.invalidate_tool_grants()
    assert permissions.tool_allowed("bash_command", "engineer", True) is True


# ---------------------------------------------------------------------------
# Nothing changes on an ordinary box
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", SECURITY_ROUTES + SYSTEM_ROUTES)
def test_an_existing_admin_keeps_everything(client, people, method, path, body):
    response = client.request(method, path, json=body, headers=_as("owner"))
    assert response.status_code not in (401, 403), response.text


def test_an_engineer_on_a_normal_box_keeps_engineer_routes(client, people):
    assert client.get("/api/env-vars", headers=_as("standard")).status_code not in (401, 403)
    assert client.post("/api/git/push", json={}, headers=_as("standard")).status_code not in (401, 403)
