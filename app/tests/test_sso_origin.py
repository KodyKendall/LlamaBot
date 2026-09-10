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
from urllib.parse import parse_qs, urlparse

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
    def __init__(self, query=None, cookies=None, headers=None):
        self.query_params = query or {}
        self.cookies = cookies or {}
        # Starlette lower-cases header names; the CTA reads "host" off this.
        self.headers = headers or {}


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


class TestLoginCtaCarriesReturnHost:
    """The CTA must tell the mothership WHICH HOST to land the user back on.

    LlamaBot's llamabot_session cookie is HOST-ONLY, but the SSO round trip always
    landed on the box's CANONICAL host. A box reached through a custom domain
    therefore got its cookie set on a hostname the user's tab was never on, so their
    own bookmark showed the login page on every visit, forever.

    Measured on box crm-4 (our own CRM) 2026-09-08: crm.llamapress.ai is a verified
    custom domain whose canonical chat host is crm-4.leo.llamapress.ai. 22 hits on
    /login and 6 complete SSO round trips in 30 hours, every grant verified 200 OK.
    Nothing in the SSO machinery was broken — it just set the cookie somewhere else.

    return_host is a SEPARATE axis from sso_origin: sso_origin picks the BRAND
    (llamapress.ai vs builtwithleo.com), return_host picks which host OF THIS BOX to
    return to. A crm.builtwithleo.com user needs both.

    Safety: the mothership validates return_host against the hosts this box verifiably
    serves (canonical, its builtwithleo mirror, or a verified CustomDomain on the chat
    port) before appending a login grant, so an unknown or forged value is ignored.
    That half is already live (2026-09-08).
    """

    def _cta(self, request, mothership_url="https://llamapress.ai", name="my-box"):
        from unittest.mock import MagicMock, patch

        from app.routers import ui

        fake = MagicMock()
        fake.mothership_url = mothership_url
        fake.instance_name = name
        with patch.object(ui, "MothershipClient", return_value=fake):
            return ui._render_sso_login_cta(request)

    def _href(self, html):
        import re
        from html import unescape

        match = re.search(r'href="([^"]*)"', html)
        assert match, f"no href in CTA: {html!r}"
        return unescape(match.group(1))

    def test_carries_the_host_the_browser_is_standing_on(self):
        html = self._cta(_FakeRequest(headers={"host": "crm.llamapress.ai"}))
        q = parse_qs(urlparse(self._href(html)).query)
        assert q["return_host"] == ["crm.llamapress.ai"]

    def test_no_host_header_still_renders_a_working_cta(self):
        # Never a broken button: the mothership falls back to Referer, then to the
        # canonical host, which is exactly today's behaviour.
        html = self._cta(_FakeRequest())
        href = self._href(html)
        assert href == "https://llamapress.ai/sso/leo/my-box"
        assert "return_host" not in href

    def test_no_request_at_all_does_not_crash(self):
        # The self-hosted path calls this with request=None.
        html = self._cta(None)
        assert "return_host" not in html
        assert "/sso/leo/my-box" in html

    def test_host_is_url_encoded(self):
        html = self._cta(_FakeRequest(headers={"host": "crm.llamapress.ai:8080"}))
        href = self._href(html)
        # The colon must not ride raw into the query string.
        assert "return_host=crm.llamapress.ai%3A8080" in href
        assert parse_qs(urlparse(href).query)["return_host"] == ["crm.llamapress.ai:8080"]

    def test_brand_and_return_host_are_independent_axes(self):
        # A builtwithleo user on a custom domain needs the builtwithleo BRAND and a
        # return_host of the domain they are actually on.
        req = _FakeRequest(
            cookies={SSO_ORIGIN_COOKIE: "https://builtwithleo.com"},
            headers={"host": "crm.builtwithleo.com"},
        )
        html = self._cta(req)
        href = self._href(html)
        assert href.startswith("https://builtwithleo.com/sso/leo/my-box")
        assert parse_qs(urlparse(href).query)["return_host"] == ["crm.builtwithleo.com"]
        assert "Sign in with your Leo account" in html

    def test_exactly_one_question_mark(self):
        href = self._href(self._cta(_FakeRequest(headers={"host": "box.example.com"})))
        assert href.count("?") == 1

    def test_href_is_still_attribute_escaped(self):
        # escape(quote=True) must stay on the href — a host is attacker-influenced.
        html = self._cta(_FakeRequest(headers={"host": 'evil"onmouseover="x'}))
        assert '"onmouseover="' not in html
