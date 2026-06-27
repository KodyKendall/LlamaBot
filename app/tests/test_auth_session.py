"""Tests for session-cookie auth and magic-link sign-in.

Run with: pytest app/tests/test_auth_session.py -v
"""
import base64
import hashlib
import hmac
import json
import os
import time

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.models import User
from app.services.token_service import (
    SESSION_COOKIE_NAME,
    create_session_token,
    verify_session_token,
)
from app.services.magic_link_service import (
    MAGIC_LINK_SECRET_ENV,
    MagicLinkInvalid,
    MagicLinkSecretMissing,
    verify_magic_link_token,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def test_user():
    return User(
        id=42,
        username="leo-abc123",
        password_hash="hashed",
        role="engineer",
        is_admin=False,
        is_active=True,
    )


@pytest.fixture
def magic_link_secret(monkeypatch):
    secret = "test-magic-link-secret-do-not-use-in-prod"
    monkeypatch.setenv(MAGIC_LINK_SECRET_ENV, secret)
    return secret


def _make_magic_token(secret: str, username: str, exp_offset: int = 240) -> str:
    """Mint a magic-link token using the same algorithm Rails will use.

    Verifies wire-format compatibility — we construct the token here using
    only stdlib (no helper from the service we're testing) and the service
    must accept it.
    """
    payload = json.dumps(
        {"username": username, "exp": int(time.time()) + exp_offset},
        separators=(",", ":"),
    )
    p64 = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), p64.encode(), hashlib.sha256).digest()
    s64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{p64}.{s64}"


# =============================================================================
# Unit: session token (PyJWT over SESSION_SECRET)
# =============================================================================

class TestSessionToken:
    def test_create_and_verify_roundtrip(self, test_user):
        token = create_session_token(test_user)
        payload = verify_session_token(token)
        assert payload is not None
        assert payload["user_id"] == 42
        assert payload["username"] == "leo-abc123"
        assert payload["type"] == "session"

    def test_verify_garbage_returns_none(self):
        assert verify_session_token("not-a-real-jwt") is None

    def test_verify_wrong_type_rejected(self, test_user):
        # Tokens of a different `type` (e.g. ws_auth) must NOT validate as a session.
        from app.services.token_service import create_ws_token
        ws_token = create_ws_token(test_user)
        assert verify_session_token(ws_token) is None


# =============================================================================
# Unit: magic-link token
# =============================================================================

class TestMagicLinkToken:
    def test_valid_token_returns_username(self, magic_link_secret):
        token = _make_magic_token(magic_link_secret, "leo-abc123")
        assert verify_magic_link_token(token) == "leo-abc123"

    def test_missing_secret_raises(self, monkeypatch):
        monkeypatch.delenv(MAGIC_LINK_SECRET_ENV, raising=False)
        token = _make_magic_token("anything", "leo-abc123")
        with pytest.raises(MagicLinkSecretMissing):
            verify_magic_link_token(token)

    def test_expired_rejected(self, magic_link_secret):
        # Make the exp far enough in the past to exceed clock-skew tolerance.
        token = _make_magic_token(magic_link_secret, "leo-abc123", exp_offset=-120)
        with pytest.raises(MagicLinkInvalid):
            verify_magic_link_token(token)

    def test_clock_skew_window(self, magic_link_secret):
        # exp=now-10s should still validate (within ±30s skew window).
        token = _make_magic_token(magic_link_secret, "leo-abc123", exp_offset=-10)
        assert verify_magic_link_token(token) == "leo-abc123"

    def test_bad_signature_rejected(self, magic_link_secret):
        token = _make_magic_token("wrong-secret", "leo-abc123")
        with pytest.raises(MagicLinkInvalid):
            verify_magic_link_token(token)

    def test_tampered_payload_rejected(self, magic_link_secret):
        token = _make_magic_token(magic_link_secret, "leo-abc123")
        p64, s64 = token.split(".")
        # Swap in a different payload but keep the original signature.
        tampered_payload = base64.urlsafe_b64encode(
            json.dumps({"username": "attacker", "exp": int(time.time()) + 240},
                       separators=(",", ":")).encode()
        ).rstrip(b"=").decode()
        with pytest.raises(MagicLinkInvalid):
            verify_magic_link_token(f"{tampered_payload}.{s64}")

    def test_malformed_token_rejected(self, magic_link_secret):
        with pytest.raises(MagicLinkInvalid):
            verify_magic_link_token("no-dot-separator")
        with pytest.raises(MagicLinkInvalid):
            verify_magic_link_token("")
        with pytest.raises(MagicLinkInvalid):
            verify_magic_link_token("a.b.c")  # too many parts

    def test_missing_username_rejected(self, magic_link_secret):
        # Hand-craft a payload missing 'username'.
        payload = json.dumps({"exp": int(time.time()) + 240}, separators=(",", ":"))
        p64 = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
        sig = hmac.new(magic_link_secret.encode(), p64.encode(), hashlib.sha256).digest()
        s64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        with pytest.raises(MagicLinkInvalid):
            verify_magic_link_token(f"{p64}.{s64}")


# =============================================================================
# Integration: routes
#
# These tests bypass the real DB by patching the user-lookup functions used
# by the routes and the auth dependency. We use a TestClient cookie jar to
# verify cookie roundtripping.
# =============================================================================

@pytest.fixture
def client_with_user(test_user):
    """TestClient where authenticate_user / get_user_by_username / get_user_by_id
    all return `test_user` when given the right credentials, and None otherwise.
    """
    from main import app

    def fake_authenticate_user(session, username, password):
        if username == test_user.username and password == "correct-password":
            return test_user
        return None

    def fake_get_user_by_username(session, username):
        if username == test_user.username:
            return test_user
        return None

    def fake_get_user_by_id(session, user_id):
        if user_id == test_user.id:
            return test_user
        return None

    # The route reads from app.routers.ui's imports; the dep reads from
    # app.dependencies's imports. Patch both.
    patches = [
        patch("app.routers.ui.authenticate_user", side_effect=fake_authenticate_user),
        patch("app.routers.ui.get_user_by_username", side_effect=fake_get_user_by_username),
        patch("app.dependencies.authenticate_user", side_effect=fake_authenticate_user),
        patch("app.dependencies.get_user_by_id", side_effect=fake_get_user_by_id),
    ]
    for p in patches:
        p.start()

    # Override DB session with a noop — none of our patched fns actually use it.
    from app.dependencies import get_db_session
    app.dependency_overrides[get_db_session] = lambda: iter([None])

    try:
        # base_url=https://… so TestClient treats the connection as secure and
        # will round-trip cookies marked Secure (which is correct prod behavior).
        with TestClient(app, base_url="https://testserver") as client:
            yield client
    finally:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(get_db_session, None)


class TestDurableSessionSecret:
    """The session-cookie secret MUST survive a container restart.

    Regression for the update-banner lockout: SESSION_SECRET used to be
    persisted to the container's ephemeral .env, so every `bin/update` recreate
    minted a new secret and silently invalidated every session cookie. Users
    arrive via a LlamaPress.ai magic-link and have no local password, so an
    invalidated session locked them out entirely. The secret is now stored in
    the auth DB (site_settings), which persists across restarts.
    """

    @staticmethod
    def _shared_sqlite_engine():
        # A single shared in-memory DB so a fresh Session() per call sees the
        # same rows (mirrors a real DB persisting across restarts).
        from sqlmodel import create_engine
        from sqlalchemy.pool import StaticPool
        return create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    def test_env_var_overrides_everything(self, monkeypatch):
        monkeypatch.setenv("SESSION_SECRET", "explicit-operator-secret")
        from app.services import token_service
        assert token_service._ensure_session_secret() == "explicit-operator-secret"

    def test_secret_is_stable_across_restarts(self, monkeypatch):
        # No env override → resolves via the DB. Two calls (= two boots) against
        # the same DB must return the SAME secret, or every cookie is invalidated.
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        eng = self._shared_sqlite_engine()
        monkeypatch.setattr("app.db.engine", eng)
        from app.services import token_service

        first = token_service._ensure_session_secret()
        assert first and len(first) >= 32

        monkeypatch.delenv("SESSION_SECRET", raising=False)  # simulate a fresh boot
        second = token_service._ensure_session_secret()
        assert second == first

    def test_secret_is_actually_persisted_to_db(self, monkeypatch):
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        eng = self._shared_sqlite_engine()
        monkeypatch.setattr("app.db.engine", eng)
        from app.services import token_service
        from sqlmodel import Session
        from app.models import SiteSetting

        secret = token_service._ensure_session_secret()
        with Session(eng) as session:
            row = session.get(SiteSetting, token_service.SESSION_SECRET_DB_KEY)
        assert row is not None and row.value == secret

    def test_no_db_falls_back_to_ephemeral_without_crashing(self, monkeypatch):
        # CI / tests with no auth DB must still boot (ephemeral, non-persistent key).
        monkeypatch.delenv("SESSION_SECRET", raising=False)
        monkeypatch.setattr("app.db.engine", None)
        from app.services import token_service
        secret = token_service._ensure_session_secret()
        assert secret and len(secret) >= 32


class TestPostLogin:
    def test_valid_credentials_set_cookie(self, client_with_user, test_user):
        resp = client_with_user.post(
            "/login",
            json={"username": test_user.username, "password": "correct-password"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        assert SESSION_COOKIE_NAME in client_with_user.cookies
        # Verify cookie contents are a valid session token for this user.
        token = client_with_user.cookies[SESSION_COOKIE_NAME]
        payload = verify_session_token(token)
        assert payload["user_id"] == test_user.id

    def test_bad_credentials_no_cookie(self, client_with_user, test_user):
        resp = client_with_user.post(
            "/login",
            json={"username": test_user.username, "password": "WRONG"},
        )
        assert resp.status_code == 401
        assert SESSION_COOKIE_NAME not in resp.cookies

    def test_form_encoded_login_works(self, client_with_user, test_user):
        # Password managers / plain <form> submits use form-encoding.
        resp = client_with_user.post(
            "/login",
            data={"username": test_user.username, "password": "correct-password"},
        )
        assert resp.status_code == 200
        assert SESSION_COOKIE_NAME in client_with_user.cookies


class TestGetLoginMagicLink:
    def test_valid_token_redirects_and_sets_cookie(
        self, client_with_user, test_user, magic_link_secret
    ):
        token = _make_magic_token(magic_link_secret, test_user.username)
        resp = client_with_user.get(f"/login?token={token}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        assert SESSION_COOKIE_NAME in resp.cookies

    def test_handoff_params_forwarded_through_redirect(
        self, client_with_user, test_user, magic_link_secret
    ):
        # Funnel hand-offs (e.g. the picture-to-html flow) pass prompt + llm_model
        # through the magic-link /login redirect. All hand-off params — including
        # llm_model, which pins a vision-capable model, and agent_mode, which pins
        # the agent persona (e.g. the excel-to-app funnel handing off in 'engineer')
        # — must survive to the chat page, or the auto-fired build runs on the wrong
        # (default) model/mode.
        from urllib.parse import urlparse, parse_qs

        token = _make_magic_token(magic_link_secret, test_user.username)
        resp = client_with_user.get(
            f"/login?token={token}"
            "&prompt=clone+this&conversation=42"
            "&welcome_prompt=hi&llm_model=gemini-3-flash&agent_mode=engineer",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        forwarded = parse_qs(urlparse(resp.headers["location"]).query)
        assert forwarded.get("prompt") == ["clone this"]
        assert forwarded.get("conversation") == ["42"]
        assert forwarded.get("welcome_prompt") == ["hi"]
        assert forwarded.get("llm_model") == ["gemini-3-flash"]
        assert forwarded.get("agent_mode") == ["engineer"]
        # token must NOT leak into the post-auth URL
        assert "token" not in forwarded

    def test_unknown_user_returns_401(
        self, client_with_user, magic_link_secret
    ):
        # Token is valid but the user doesn't exist — locked behavior: 401, no auto-provision.
        token = _make_magic_token(magic_link_secret, "user-does-not-exist")
        resp = client_with_user.get(f"/login?token={token}", follow_redirects=False)
        assert resp.status_code == 401

    def test_expired_token_returns_401(
        self, client_with_user, test_user, magic_link_secret
    ):
        token = _make_magic_token(magic_link_secret, test_user.username, exp_offset=-120)
        resp = client_with_user.get(f"/login?token={token}", follow_redirects=False)
        assert resp.status_code == 401

    def test_bad_signature_returns_401(
        self, client_with_user, test_user, magic_link_secret
    ):
        token = _make_magic_token("wrong-secret", test_user.username)
        resp = client_with_user.get(f"/login?token={token}", follow_redirects=False)
        assert resp.status_code == 401

    def test_missing_secret_returns_503(
        self, client_with_user, test_user, monkeypatch
    ):
        monkeypatch.delenv(MAGIC_LINK_SECRET_ENV, raising=False)
        # Token doesn't matter — we never get past the secret check.
        resp = client_with_user.get(
            "/login?token=anything.anything", follow_redirects=False
        )
        assert resp.status_code == 503

    def test_missing_token_serves_form_not_400(self, client_with_user):
        # GET /login with no token now serves the HTML sign-in form
        # (covered in TestLoginForm). It must NOT 400 — that would break the
        # browser-user front door.
        with patch("app.routers.ui.has_any_users", return_value=True), \
             patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = "ok"
            resp = client_with_user.get("/login", follow_redirects=False)
        assert resp.status_code == 200


class TestCookieAuthOnProtectedRoute:
    """The new cookie-or-Basic dep must accept session cookies AND keep
    Basic Auth working — the existing 27 call sites depend on that."""

    def _do_login_get_cookie(self, client, test_user):
        resp = client.post(
            "/login",
            json={"username": test_user.username, "password": "correct-password"},
        )
        assert resp.status_code == 200

    def test_cookie_grants_access_without_authorization_header(
        self, client_with_user, test_user
    ):
        self._do_login_get_cookie(client_with_user, test_user)
        # Hit a route that uses Depends(get_current_user). /settings is one.
        resp = client_with_user.get("/settings")
        assert resp.status_code == 200, resp.text

    def test_basic_auth_still_works(self, client_with_user, test_user):
        # No cookie set; supply Basic creds. Must pass.
        creds = base64.b64encode(
            f"{test_user.username}:correct-password".encode()
        ).decode()
        resp = client_with_user.get(
            "/settings", headers={"Authorization": f"Basic {creds}"}
        )
        assert resp.status_code == 200, resp.text

    def test_no_auth_returns_401(self, client_with_user):
        client_with_user.cookies.clear()
        resp = client_with_user.get("/settings")
        assert resp.status_code == 401
        # The 401 must include WWW-Authenticate so curl/HTTP clients can prompt.
        assert "basic" in resp.headers.get("www-authenticate", "").lower()


class TestLoginForm:
    """The HTML sign-in form replaces the browser's native Basic Auth dialog."""

    def test_get_login_no_token_serves_html_form(self, client_with_user):
        with patch("app.routers.ui.has_any_users", return_value=True), \
             patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = (
                "<form id='loginForm'></form>"
            )
            resp = client_with_user.get("/login", follow_redirects=False)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "loginForm" in resp.text

    def test_get_login_no_users_redirects_to_register(self, client_with_user):
        with patch("app.routers.ui.has_any_users", return_value=False):
            resp = client_with_user.get("/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/register"

    def test_root_unauth_redirects_to_login_not_401(self, client_with_user):
        """The headline UX win: visiting / with no creds should NOT trigger
        the browser's Basic Auth dialog (which is what 401 + WWW-Authenticate
        produces). Instead, redirect to the HTML form."""
        client_with_user.cookies.clear()
        with patch("app.routers.ui.has_any_users", return_value=True):
            resp = client_with_user.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"
        # Critically: no WWW-Authenticate header, so no browser dialog.
        assert "www-authenticate" not in {k.lower() for k in resp.headers.keys()}


class TestLogout:
    def test_logout_clears_cookie_and_returns_401(
        self, client_with_user, test_user
    ):
        # Log in first to seed the cookie.
        client_with_user.post(
            "/login",
            json={"username": test_user.username, "password": "correct-password"},
        )
        assert SESSION_COOKIE_NAME in client_with_user.cookies

        resp = client_with_user.post("/logout")
        assert resp.status_code == 401
        assert "basic" in resp.headers.get("www-authenticate", "").lower()
        # Cookie should be cleared in the response (Set-Cookie with Max-Age=0).
        set_cookie = resp.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in set_cookie
