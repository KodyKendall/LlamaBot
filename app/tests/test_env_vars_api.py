"""Disclosure and access rules for the environment settings endpoints.

The property that matters most is negative: **no endpoint returns anything about
the .env file** — not a value, not a key name, not whether a key is set. The file
holds every provider API key, the database URLs, the SSO login secret and the VS
Code password (shell access to the box).

The secondary properties are the two narrow write paths: an allowlisted boolean
toggle that coerces its value, and custom variables that can never override an
existing setting.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.dependencies import admin_required, engineer_or_admin_required, get_db_session
from app.models import User
from app.services import env_settings_service as svc


ADMIN = User(id=1, username="root-admin", password_hash="h", role="engineer",
             is_admin=True, is_active=True)
ENGINEER = User(id=2, username="eng", password_hash="h", role="engineer",
                is_admin=False, is_active=True)

API_KEY_VALUE = "sk-supersecret-abcdef123456"
VSCODE_VALUE = "hunter2-shell-access"
DB_VALUE = "postgresql://user:pw@db/llamabot"

#: Every string that must never appear in any response body.
SECRETS = (API_KEY_VALUE, VSCODE_VALUE, DB_VALUE)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        f'OPENAI_API_KEY="{API_KEY_VALUE}"\n'
        f'VSCODE_PASSWORD="{VSCODE_VALUE}"\n'
        f"DB_URI={DB_VALUE}\n"
        "MODEL_SWITCHING_ALLOWED=false\n"
    )
    monkeypatch.setenv("LEONARDO_ENV_FILE", str(path))
    return path


@pytest.fixture
def client(db_session, env_file):
    """TestClient authenticated as an admin."""
    from main import app

    app.dependency_overrides[admin_required] = lambda: ADMIN
    app.dependency_overrides[engineer_or_admin_required] = lambda: ADMIN
    app.dependency_overrides[get_db_session] = lambda: db_session
    try:
        with TestClient(app, base_url="https://testserver") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def engineer_client(db_session, env_file):
    """TestClient authenticated as a non-admin engineer."""
    from main import app

    app.dependency_overrides[engineer_or_admin_required] = lambda: ENGINEER
    app.dependency_overrides[get_db_session] = lambda: db_session
    try:
        with TestClient(app, base_url="https://testserver") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Nothing about the .env is disclosed
# --------------------------------------------------------------------------

def test_settings_payload_contains_no_secret_values(client):
    body = client.get("/api/env-vars").text
    for secret in SECRETS:
        assert secret not in body


def test_settings_payload_contains_no_env_key_names(client):
    """Not even the names — knowing VSCODE_PASSWORD exists is itself a leak."""
    body = client.get("/api/env-vars").text
    for name in ("OPENAI_API_KEY", "VSCODE_PASSWORD", "DB_URI"):
        assert name not in body


def test_settings_payload_does_not_leak_the_env_file_path(client):
    assert "file" not in client.get("/api/env-vars").json()


def test_settings_payload_exposes_only_the_expected_fields(client):
    """Pins the shape, so a future field can't quietly reintroduce disclosure."""
    body = client.get("/api/env-vars").json()
    assert set(body) == {"writable", "can_edit", "toggles", "custom_vars", "pending"}
    assert {t["key"] for t in body["toggles"]} == set(svc.TOGGLE_KEYS)


def test_the_reveal_endpoint_no_longer_exists(client):
    resp = client.get("/api/env-vars/OPENAI_API_KEY/reveal")
    assert resp.status_code in (404, 405)
    for secret in SECRETS:
        assert secret not in resp.text


def test_there_is_no_arbitrary_write_endpoint(client, env_file):
    resp = client.put("/api/env-vars/OPENAI_API_KEY", json={"value": "sk-attacker"})
    assert resp.status_code in (404, 405)
    assert "sk-attacker" not in env_file.read_text()


def test_no_route_can_return_the_env_file(client):
    """Belt and braces: no registered path mentions reveal."""
    from main import app
    assert not [r for r in app.routes if "reveal" in getattr(r, "path", "")]


def test_model_list_does_not_name_env_vars(client, monkeypatch):
    """/api/available-models is readable by every signed-in user."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    body = client.get("/api/available-models").text

    assert "ANTHROPIC_API_KEY" not in body
    assert "not configured in .env" not in body


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path,method,guard",
    [
        ("/api/env-vars", "GET", "engineer_or_admin_required"),
        ("/api/env-vars/pending", "GET", "engineer_or_admin_required"),
        ("/api/env-toggles/{key}", "PUT", "engineer_or_admin_required"),
        ("/api/custom-env-vars", "POST", "admin_required"),
        ("/api/custom-env-vars/{name}", "DELETE", "admin_required"),
    ],
)
def test_routes_keep_their_intended_guard(path, method, guard):
    """Pins each gate so loosening one has to be deliberate."""
    from main import app

    route = next(
        r for r in app.routes
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set())
    )
    names = {d.call.__name__ for d in route.dependant.dependencies if d.call}
    assert guard in names, f"{method} {path} should be gated by {guard}, got {names}"


def test_engineer_cannot_manage_custom_vars(engineer_client):
    """admin_required is not overridden here, so the real gate runs."""
    resp = engineer_client.post("/api/custom-env-vars", json={"name": "X_Y", "value": "1"})
    assert resp.status_code in (401, 403)


# --------------------------------------------------------------------------
# Toggles
# --------------------------------------------------------------------------

def test_toggle_flips_and_applies_immediately(client, env_file, monkeypatch):
    monkeypatch.delenv("MODEL_SWITCHING_ALLOWED", raising=False)
    body = client.put("/api/env-toggles/MODEL_SWITCHING_ALLOWED", json={"value": True}).json()

    assert body["enabled"] is True
    assert body["needs_restart"] is False
    assert 'MODEL_SWITCHING_ALLOWED="true"' in env_file.read_text()


@pytest.mark.parametrize("key", [
    "OPENAI_API_KEY", "VSCODE_PASSWORD", "DB_URI", "PAYWALL_ENABLED",
    "WS_AUTH_REQUIRED", "ENABLE_GITHUB_BUTTON", "LLAMABOT_COOKIE_SECURE",
])
def test_toggle_endpoint_refuses_everything_outside_the_allowlist(client, env_file, key):
    original = env_file.read_text()
    resp = client.put("/api/env-toggles/" + key, json={"value": True})

    assert resp.status_code == 400
    assert env_file.read_text() == original


@pytest.mark.parametrize("payload", ["true\nDB_URI=evil", "sk-attacker", 12345, {"a": 1}])
def test_toggle_value_is_coerced_not_written(client, env_file, payload):
    client.put("/api/env-toggles/MODEL_SWITCHING_ALLOWED", json={"value": payload})
    text = env_file.read_text()

    assert "evil" not in text
    assert "sk-attacker" not in text


# --------------------------------------------------------------------------
# Custom variables
# --------------------------------------------------------------------------

def test_custom_var_is_created_and_rendered(client, env_file):
    resp = client.post("/api/custom-env-vars",
                       json={"name": "MY_SERVICE_URL", "value": "https://example.test"})
    assert resp.status_code == 200
    assert resp.json()["needs_restart"] is True

    text = env_file.read_text()
    assert svc.MANAGED_BEGIN in text
    assert 'MY_SERVICE_URL="https://example.test"' in text


def test_custom_var_value_is_never_returned(client):
    client.post("/api/custom-env-vars",
                json={"name": "MY_SERVICE_URL", "value": "top-secret-endpoint"})
    body = client.get("/api/env-vars")

    assert "top-secret-endpoint" not in body.text
    entry = next(v for v in body.json()["custom_vars"] if v["name"] == "MY_SERVICE_URL")
    assert entry["masked"] == svc.MASK


@pytest.mark.parametrize("name", [
    "OPENAI_API_KEY", "VSCODE_PASSWORD", "DB_URI", "SESSION_SECRET",
    "MODEL_SWITCHING_ALLOWED", "AWS_KEY", "SCHEDULER_TOKEN",
])
def test_reserved_names_are_refused(client, env_file, name):
    original = env_file.read_text()
    resp = client.post("/api/custom-env-vars", json={"name": name, "value": "sk-attacker"})

    assert resp.status_code == 400
    assert env_file.read_text() == original


def test_every_rejection_uses_the_same_message(client):
    """Otherwise the endpoint becomes an oracle for what this box has configured."""
    configured = client.post("/api/custom-env-vars",
                             json={"name": "OPENAI_API_KEY", "value": "x"}).json()["detail"]
    not_configured = client.post("/api/custom-env-vars",
                                 json={"name": "STRIPE_API_KEY", "value": "x"}).json()["detail"]

    assert configured == not_configured == svc.RESERVED_MESSAGE


def test_newline_injection_is_refused(client, env_file):
    resp = client.post("/api/custom-env-vars",
                       json={"name": "OK_NAME", "value": "ok\nDB_URI=evil"})
    assert resp.status_code == 400
    assert "evil" not in env_file.read_text()


def test_custom_var_is_deleted_and_unrendered(client, env_file):
    client.post("/api/custom-env-vars", json={"name": "TEMP_VAR", "value": "1"})
    assert "TEMP_VAR" in env_file.read_text()

    assert client.delete("/api/custom-env-vars/TEMP_VAR").status_code == 200
    assert "TEMP_VAR" not in env_file.read_text()


def test_deleting_an_unknown_custom_var_is_404(client):
    assert client.delete("/api/custom-env-vars/NOPE").status_code == 404


# --------------------------------------------------------------------------
# Pending-restart tracking
# --------------------------------------------------------------------------

def test_custom_var_marks_a_pending_restart(client, db_session):
    from app.services import env_store

    client.post("/api/custom-env-vars", json={"name": "MY_SERVICE_URL", "value": "x"})
    assert env_store.pending_summary(db_session)["restart_required"] is True


def test_pending_records_no_value(client, db_session):
    from app.models import SiteSetting
    from app.services import env_store

    client.post("/api/custom-env-vars",
                json={"name": "MY_SERVICE_URL", "value": "brand-new-secret-value"})
    row = db_session.get(SiteSetting, env_store.PENDING_KEY)

    assert row is not None
    assert "brand-new-secret-value" not in row.value


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "user,expected",
    [
        (ADMIN, 200),
        (ENGINEER, 200),
        (User(id=3, username="plain", password_hash="h", role="user",
              is_admin=False, is_active=True), 403),
    ],
)
def test_environment_page_is_gated(user, expected, env_file):
    from app.dependencies import get_current_user
    from main import app

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with TestClient(app, base_url="https://testserver") as c:
            assert c.get("/settings/environment").status_code == expected
    finally:
        app.dependency_overrides.clear()


def test_environment_page_carries_no_secrets(env_file):
    from app.dependencies import get_current_user
    from main import app

    app.dependency_overrides[get_current_user] = lambda: ADMIN
    try:
        with TestClient(app, base_url="https://testserver") as c:
            body = c.get("/settings/environment").text
    finally:
        app.dependency_overrides.clear()

    assert "window.LLAMABOT_IS_ADMIN = true;" in body
    for secret in SECRETS:
        assert secret not in body
