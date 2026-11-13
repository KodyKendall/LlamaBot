"""LP-30: /api/ws-token must never be served from a browser or proxy cache.

A reopened (discarded or restored) chat tab got its retry token from the browser's cache:
the box logged "Token rejected: expired" twice with no /api/ws-token request between them.
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from main import app
from app.dependencies import get_current_user


def _fake_user():
    return SimpleNamespace(
        id=7, username="kody", role="user", is_admin=False,
        is_security_admin=False, is_system_admin=False,
    )


def test_ws_token_response_forbids_caching():
    app.dependency_overrides[get_current_user] = _fake_user
    try:
        with TestClient(app) as client:
            response = client.get("/api/ws-token")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["token"] and body["expires_in"] > 0


def test_each_call_issues_a_new_token():
    app.dependency_overrides[get_current_user] = _fake_user
    try:
        with TestClient(app) as client:
            first = client.get("/api/ws-token").json()["token"]
            import time
            time.sleep(1.1)  # iat has one-second resolution
            second = client.get("/api/ws-token").json()["token"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert first != second
