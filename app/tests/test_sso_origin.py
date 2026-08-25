"""Tests for the SSO return-to-brand contract (app/services/sso_origin.py).

The bug: a user who signed in at builtwithleo.com and later needed to
re-authorize was bounced to llamapress.ai — the domain baked into
instance.json — and hit a sign-in wall on a brand they'd never used.

Covered here:
  * sanitize_sso_origin allowlists hosts (open-redirect guard);
  * resolve_sso_origin precedence: query param > cookie > configured mothership;
  * GET /auth/consume remembers the origin and bounces back to it;
  * the /login SSO CTA points at the remembered brand, with its copy.

Run with: pytest app/tests/test_sso_origin.py -v
"""
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app.services.sso_origin import (
    SSO_ORIGIN_COOKIE,
    brand_display_name,
    remember_sso_origin,
    resolve_sso_origin,
    sanitize_sso_origin,
)

MOTHERSHIP = "https://llamapress.ai"


class _FakeRequest:
    def __init__(self, query=None, cookies=None):
        self.query_params = query or {}
        self.cookies = cookies or {}


class _FakeResponse:
    def __init__(self):
        self.cookies_set = {}

    def set_cookie(self, key, value, **kwargs):
        self.cookies_set[key] = (value, kwargs)


@pytest.fixture
def db_engine(monkeypatch):
    """Fresh shared in-memory sqlite bound into the router (mirrors
    test_unified_login.py — the /auth/consume route reads its module-level
    engine)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.routers.unified_login.engine", engine)
    monkeypatch.setattr("app.db.engine", engine)
    return engine


@pytest.fixture
def client():
    # No `with` → skip the app's startup/shutdown (DB init, graph compile).
    from main import app
    return TestClient(app, base_url="https://testserver")


# =============================================================================
# sanitize_sso_origin
# =============================================================================

class TestSanitize:
    @pytest.mark.parametrize("value,expected", [
        ("https://builtwithleo.com", "https://builtwithleo.com"),
        ("builtwithleo.com", "https://builtwithleo.com"),
        ("https://www.builtwithleo.com/anything?x=1", "https://www.builtwithleo.com"),
        ("https://llamapress.ai/", "https://llamapress.ai"),
        ("HTTPS://BuiltWithLeo.com", "https://builtwithleo.com"),
    ])
    def test_accepts_allowlisted_brands(self, value, expected):
        assert sanitize_sso_origin(value, MOTHERSHIP) == expected

    @pytest.mark.parametrize("value", [
        "",
        None,
        "https://evil.example.com",
        # Lookalikes that a naive substring check would wave through.
        "https://builtwithleo.com.evil.com",
        "https://notbuiltwithleo.com",
        "javascript:alert(1)",
        "//evil.example.com",
    ])
    def test_rejects_everything_else(self, value):
        assert sanitize_sso_origin(value, MOTHERSHIP) is None

    def test_configured_mothership_is_always_allowed(self):
        assert sanitize_sso_origin(
            "https://mothership.test", "https://mothership.test"
        ) == "https://mothership.test"
        # ...and is NOT allowed for a box configured elsewhere.
        assert sanitize_sso_origin("https://mothership.test", MOTHERSHIP) is None

    def test_env_var_extends_the_allowlist(self, monkeypatch):
        assert sanitize_sso_origin("https://staging.leo.dev", MOTHERSHIP) is None
        monkeypatch.setenv("SSO_ORIGIN_HOSTS", "leo.dev, other.test")
        assert sanitize_sso_origin("https://staging.leo.dev", MOTHERSHIP) == \
            "https://staging.leo.dev"

    def test_http_on_a_public_brand_is_upgraded(self):
        assert sanitize_sso_origin("http://builtwithleo.com", MOTHERSHIP) == \
            "https://builtwithleo.com"

    def test_explicit_port_survives(self, monkeypatch):
        monkeypatch.setenv("SSO_ORIGIN_HOSTS", "localhost")
        assert sanitize_sso_origin("http://localhost:3000", MOTHERSHIP) == \
            "http://localhost:3000"


# =============================================================================
# resolve / remember
# =============================================================================

class TestResolve:
    def test_query_param_wins(self):
        req = _FakeRequest(
            query={"sso_origin": "https://builtwithleo.com"},
            cookies={SSO_ORIGIN_COOKIE: "https://llamapress.ai"},
        )
        assert resolve_sso_origin(req, MOTHERSHIP) == "https://builtwithleo.com"

    def test_falls_back_to_cookie(self):
        req = _FakeRequest(cookies={SSO_ORIGIN_COOKIE: "https://builtwithleo.com"})
        assert resolve_sso_origin(req, MOTHERSHIP) == "https://builtwithleo.com"

    def test_falls_back_to_configured_mothership(self):
        assert resolve_sso_origin(_FakeRequest(), MOTHERSHIP) == MOTHERSHIP

    def test_poisoned_cookie_falls_back_to_mothership(self):
        req = _FakeRequest(cookies={SSO_ORIGIN_COOKIE: "https://evil.example.com"})
        assert resolve_sso_origin(req, MOTHERSHIP) == MOTHERSHIP

    def test_none_when_no_mothership_configured(self):
        assert resolve_sso_origin(_FakeRequest(), "") is None

    def test_remember_sets_cookie_for_valid_origin(self):
        req = _FakeRequest(query={"sso_origin": "https://builtwithleo.com"})
        resp = _FakeResponse()
        remember_sso_origin(req, resp, MOTHERSHIP)
        value, kwargs = resp.cookies_set[SSO_ORIGIN_COOKIE]
        assert value == "https://builtwithleo.com"
        assert kwargs["httponly"] is True
        assert kwargs["samesite"] == "lax"

    def test_remember_ignores_a_bogus_origin(self):
        req = _FakeRequest(query={"sso_origin": "https://evil.example.com"})
        resp = _FakeResponse()
        remember_sso_origin(req, resp, MOTHERSHIP)
        assert resp.cookies_set == {}


class TestBrandDisplayName:
    @pytest.mark.parametrize("origin,expected", [
        ("https://builtwithleo.com", "Leo"),
        ("https://www.builtwithleo.com", "Leo"),
        ("https://llamapress.ai", "LlamaPress.ai"),
        ("https://mothership.test", "mothership.test"),
    ])
    def test_labels(self, origin, expected):
        assert brand_display_name(origin) == expected


# =============================================================================
# Wiring: /auth/consume and the /login CTA
# =============================================================================

class TestConsumeHonorsOrigin:
    def test_bounce_goes_to_the_origin_brand(self, db_engine, client, monkeypatch):
        """A recoverable grant failure re-authorizes at builtwithleo.com."""
        from app.tests.test_unified_login import FakeMothership, _install_mothership

        fake = FakeMothership(error_code="grant_expired")
        with _install_mothership(fake):
            resp = client.get(
                "/auth/consume?token=T&sso_origin=https://builtwithleo.com",
                follow_redirects=False,
            )
        assert resp.status_code == 302
        loc = urlparse(resp.headers["location"])
        assert loc.netloc == "builtwithleo.com"
        assert loc.path == "/sso/leo/my-box"

    def test_bounce_defaults_to_configured_mothership(self, db_engine, client):
        from app.tests.test_unified_login import FakeMothership, _install_mothership

        fake = FakeMothership(error_code="grant_expired")
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        loc = urlparse(resp.headers["location"])
        assert loc.netloc == "mothership.test"

    def test_bad_origin_cannot_aim_the_bounce(self, db_engine, client):
        from app.tests.test_unified_login import FakeMothership, _install_mothership

        fake = FakeMothership(error_code="grant_expired")
        with _install_mothership(fake):
            resp = client.get(
                "/auth/consume?token=T&sso_origin=https://evil.example.com",
                follow_redirects=False,
            )
        loc = urlparse(resp.headers["location"])
        assert loc.netloc == "mothership.test"

    def test_successful_consume_remembers_the_origin(self, db_engine, client):
        from app.tests.test_unified_login import (
            SUCCESS_BODY, FakeMothership, _install_mothership,
        )

        fake = FakeMothership(payload=SUCCESS_BODY)
        with _install_mothership(fake):
            resp = client.get(
                "/auth/consume?token=T&sso_origin=https://builtwithleo.com",
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert SSO_ORIGIN_COOKIE in resp.headers.get("set-cookie", "")
        # ...and the hand-off URL doesn't leak the param into the chat.
        assert "sso_origin" not in resp.headers["location"]


class TestLoginCtaHonorsOrigin:
    def _cta(self, request, mothership_url="https://llamapress.ai", name="my-box"):
        from unittest.mock import MagicMock, patch

        from app.routers import ui

        fake = MagicMock()
        fake.mothership_url = mothership_url
        fake.instance_name = name
        with patch.object(ui, "MothershipClient", return_value=fake):
            return ui._render_sso_login_cta(request)

    def test_uses_remembered_brand(self):
        req = _FakeRequest(cookies={SSO_ORIGIN_COOKIE: "https://builtwithleo.com"})
        html = self._cta(req)
        assert "https://builtwithleo.com/sso/leo/my-box" in html
        assert "Sign in with your Leo account" in html
        assert "llamapress.ai" not in html

    def test_defaults_to_the_configured_mothership(self):
        html = self._cta(_FakeRequest())
        assert "https://llamapress.ai/sso/leo/my-box" in html
        assert "Sign in with your LlamaPress.ai account" in html

    def test_empty_on_self_hosted(self):
        assert self._cta(_FakeRequest(), mothership_url="", name="") == ""
