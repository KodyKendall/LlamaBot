"""Tests for Unified Login Phase 2 — MothershipClient.verify_login_grant and the
GET /auth/consume flow (shadow users, retry bounce breaker, graceful failures).

Run with: pytest app/tests/test_unified_login.py -v
"""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import User
from app.services.token_service import SESSION_COOKIE_NAME, create_session_token
from app.services.user_service import hash_password


# =============================================================================
# Unit: MothershipClient.verify_login_grant
# =============================================================================

FAKE_CONFIG = {
    "instance_name": "my-box",
    "mothership_url": "https://mothership.test",
    "mothership_api_token": "tok-test",
}

SUCCESS_BODY = {
    "success": True,
    "user": {"guid": "guid-abc-123", "email": "owner@example.com", "name": "Jane Doe"},
    "instance": {"guid": "my-box"},
    "role": "owner",
    "permissions": ["chat", "rails_app"],
    "link_username": "leo-xxxxxx",
}


def _make_client():
    from app.services.mothership_client import MothershipClient
    client = MothershipClient.__new__(MothershipClient)
    client.config = FAKE_CONFIG
    return client


def _mock_http(response=None, *, raises=None):
    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    if raises is not None:
        mock_http.post = AsyncMock(side_effect=raises)
    else:
        mock_http.post = AsyncMock(return_value=response)
    return mock_http


def _resp(status, body):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body
    return resp


class TestVerifyLoginGrant:
    @pytest.mark.asyncio
    async def test_success_returns_payload_no_error(self):
        client = _make_client()
        mock_http = _mock_http(_resp(200, SUCCESS_BODY))
        with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
            payload, code = await client.verify_login_grant("raw-token", "llamabot")
        assert code is None
        assert payload["user"]["guid"] == "guid-abc-123"
        # Correct request contract.
        sent = mock_http.post.call_args.kwargs["json"]
        assert sent == {"instance_name": "my-box", "token": "raw-token", "audience": "llamabot"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status,code", [
        (404, "grant_not_found"),
        (410, "grant_expired"),
        (409, "grant_used"),
        (422, "bad_audience"),
    ])
    async def test_server_error_codes_pass_through(self, status, code):
        client = _make_client()
        mock_http = _mock_http(_resp(status, {"success": False, "error_code": code}))
        with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
            payload, err = await client.verify_login_grant("t", "llamabot")
        assert payload is None
        assert err == code

    @pytest.mark.asyncio
    async def test_transport_error_maps_to_unreachable(self):
        client = _make_client()
        mock_http = _mock_http(raises=httpx.ConnectError("refused"))
        with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
            payload, err = await client.verify_login_grant("t", "llamabot")
        assert payload is None
        assert err == "mothership_unreachable"

    @pytest.mark.asyncio
    async def test_401_creds_drift_maps_to_unreachable(self):
        client = _make_client()
        mock_http = _mock_http(_resp(401, {}))
        with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http):
            payload, err = await client.verify_login_grant("t", "llamabot")
        assert (payload, err) == (None, "mothership_unreachable")

    @pytest.mark.asyncio
    async def test_disabled_client_is_unreachable(self):
        from app.services.mothership_client import MothershipClient
        client = MothershipClient.__new__(MothershipClient)
        client.config = None
        payload, err = await client.verify_login_grant("t", "llamabot")
        assert (payload, err) == (None, "mothership_unreachable")


# =============================================================================
# Integration: GET /auth/consume
# =============================================================================

class FakeMothership:
    """Stand-in for MothershipClient the /auth/consume route constructs."""

    def __init__(self, *, payload=None, error_code=None,
                 instance_name="my-box", mothership_url="https://mothership.test"):
        self._payload = payload
        self._error = error_code
        self.enabled = True
        self.instance_name = instance_name
        self.mothership_url = mothership_url
        self.reported = []

    async def verify_login_grant(self, token, audience="llamabot"):
        return self._payload, self._error

    async def report_error(self, **kwargs):
        self.reported.append(kwargs)
        return None


@pytest.fixture
def db_engine(monkeypatch):
    """Fresh shared in-memory sqlite bound into the router as its engine."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    # The router does `Session(engine)` off its module-level import; patch that.
    monkeypatch.setattr("app.routers.unified_login.engine", engine)
    # _user_from_session_cookie resolves via app.db.engine indirectly? No — it
    # uses the passed session. But keep app.db.engine consistent for safety.
    monkeypatch.setattr("app.db.engine", engine)
    return engine


@pytest.fixture
def client():
    # No `with` → skip the app's startup/shutdown (DB init, graph compile).
    from main import app
    return TestClient(app, base_url="https://testserver")


def _install_mothership(fake):
    return patch("app.routers.unified_login.MothershipClient", return_value=fake)


def _seed_user(engine, **kwargs):
    defaults = dict(
        username="seed",
        password_hash=hash_password("x"),
        role="engineer",
        is_admin=False,
        is_active=True,
    )
    defaults.update(kwargs)
    with Session(engine) as s:
        u = User(**defaults)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def _users(engine):
    with Session(engine) as s:
        return list(s.exec(select(User)).all())


class TestConsumeHappyPath:
    def test_creates_user_sets_cookie_and_threads_token(self, db_engine, client):
        fake = FakeMothership(payload=SUCCESS_BODY)
        with _install_mothership(fake):
            resp = client.get(
                "/auth/consume?token=RAW123&prompt=build+me&llm_model=deepseek-v4-flash"
                "&agent_mode=engineer",
                follow_redirects=False,
            )
        assert resp.status_code == 302
        loc = urlparse(resp.headers["location"])
        assert loc.path == "/"
        q = parse_qs(loc.query)
        # Same raw token threaded to the iframe, plus the hand-off params.
        assert q["rails_token"] == ["RAW123"]
        assert q["prompt"] == ["build me"]
        assert q["llm_model"] == ["deepseek-v4-flash"]
        assert q["agent_mode"] == ["engineer"]
        # Session cookie set.
        assert SESSION_COOKIE_NAME in resp.headers.get("set-cookie", "")
        # Shadow user provisioned by guid.
        users = _users(db_engine)
        assert len(users) == 1
        assert users[0].llamapress_user_guid == "guid-abc-123"
        assert users[0].email == "owner@example.com"

    def test_honors_sanitized_return_to(self, db_engine, client):
        fake = FakeMothership(payload=SUCCESS_BODY)
        with _install_mothership(fake):
            resp = client.get(
                "/auth/consume?token=T&return_to=/dashboard",
                follow_redirects=False,
            )
        loc = urlparse(resp.headers["location"])
        assert loc.path == "/dashboard"

    def test_rejects_open_redirect_return_to(self, db_engine, client):
        fake = FakeMothership(payload=SUCCESS_BODY)
        with _install_mothership(fake):
            resp = client.get(
                "/auth/consume?token=T&return_to=https://evil.example.com",
                follow_redirects=False,
            )
        loc = urlparse(resp.headers["location"])
        assert loc.netloc == ""
        assert loc.path == "/"


class TestLinkingMatrix:
    def _payload(self, guid="guid-abc-123", link_username=None, role="owner"):
        p = {
            "success": True,
            "user": {"guid": guid, "email": "o@e.com", "name": "Owner"},
            "role": role,
        }
        if link_username:
            p["link_username"] = link_username
        return p

    def test_guid_match_reuses_and_syncs(self, db_engine, client):
        existing = _seed_user(db_engine, username="lp-guid-abc-123",
                              llamapress_user_guid="guid-abc-123", email="old@e.com")
        fake = FakeMothership(payload=self._payload())
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        assert resp.status_code == 302
        users = _users(db_engine)
        assert len(users) == 1  # no new user
        assert users[0].id == existing.id
        assert users[0].email == "o@e.com"  # synced

    def test_link_username_one_time_adoption(self, db_engine, client):
        # The claim-time admin account exists with NO guid yet.
        admin = _seed_user(db_engine, username="leo-admin", is_admin=True,
                           llamapress_user_guid=None)
        fake = FakeMothership(payload=self._payload(link_username="leo-admin"))
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        assert resp.status_code == 302
        users = _users(db_engine)
        assert len(users) == 1  # adopted, not created
        assert users[0].username == "leo-admin"
        assert users[0].llamapress_user_guid == "guid-abc-123"

    def test_link_username_bound_to_other_guid_creates_fresh(self, db_engine, client):
        _seed_user(db_engine, username="leo-admin", llamapress_user_guid="OTHER-guid")
        fake = FakeMothership(payload=self._payload(link_username="leo-admin"))
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        assert resp.status_code == 302
        users = {u.username: u for u in _users(db_engine)}
        assert "leo-admin" in users
        # A fresh shadow user was created for the new guid.
        fresh = [u for u in users.values() if u.llamapress_user_guid == "guid-abc-123"]
        assert len(fresh) == 1
        assert fresh[0].username == "o@e.com"
        # And the integrity smell was reported.
        assert any(r["error_class"] == "UnifiedLogin::ShadowUserError" for r in fake.reported)

    def test_no_link_username_creates_user_named_by_verified_email(self, db_engine, client):
        """Shadow username is the mothership-VERIFIED email, not lp-<guid>.

        The email arrives over the server-to-server bearer channel after
        grant_redeemer succeeds, so it is not user input. A recognizable username
        beats `lp-a1b2c3d4e5f6` everywhere the operator sees it.
        """
        fake = FakeMothership(payload=self._payload(link_username=None))
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        assert resp.status_code == 302
        users = _users(db_engine)
        assert len(users) == 1
        assert users[0].username == "o@e.com"
        assert users[0].email == "o@e.com"
        assert users[0].llamapress_user_guid == "guid-abc-123"

    def test_falls_back_to_lp_guid_when_the_grant_has_no_email(self, db_engine, client):
        payload = self._payload(link_username=None)
        payload["user"].pop("email")
        fake = FakeMothership(payload=payload)
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        assert resp.status_code == 302
        users = _users(db_engine)
        assert len(users) == 1
        assert users[0].username == "lp-guid-abc-123"

    def test_username_collision_gets_a_suffix(self, db_engine, client):
        """Someone already owns that username locally — don't hijack it."""
        _seed_user(db_engine, username="o@e.com", llamapress_user_guid=None)
        fake = FakeMothership(payload=self._payload(link_username=None))
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        assert resp.status_code == 302
        fresh = [u for u in _users(db_engine) if u.llamapress_user_guid == "guid-abc-123"]
        assert len(fresh) == 1
        assert fresh[0].username.startswith("o@e.com-")
        assert fresh[0].username != "o@e.com"

    def test_existing_lp_guid_users_are_never_renamed(self, db_engine, client):
        """Step-1 guid match only syncs email/display_name — it must not rename."""
        _seed_user(db_engine, username="lp-guid-abc-123",
                   llamapress_user_guid="guid-abc-123", email="old@e.com")
        fake = FakeMothership(payload=self._payload())
        with _install_mothership(fake):
            client.get("/auth/consume?token=T", follow_redirects=False)
        users = _users(db_engine)
        assert len(users) == 1
        assert users[0].username == "lp-guid-abc-123"
        assert users[0].email == "o@e.com"


class TestFailurePaths:
    def test_replay_with_valid_session_silently_redirects_home(self, db_engine, client):
        user = _seed_user(db_engine, username="lp-existing",
                         llamapress_user_guid="guid-existing")
        cookie = create_session_token(user)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        fake = FakeMothership(error_code="grant_used")
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=STALE", follow_redirects=False)
        assert resp.status_code == 302
        assert urlparse(resp.headers["location"]).path == "/"
        # A stale token on refresh is NOT an error — nothing reported.
        assert fake.reported == []

    def test_expired_no_session_no_retry_bounces_to_sso(self, db_engine, client):
        fake = FakeMothership(error_code="grant_expired")
        with _install_mothership(fake):
            resp = client.get(
                "/auth/consume?token=OLD&prompt=hi", follow_redirects=False
            )
        assert resp.status_code == 302
        loc = urlparse(resp.headers["location"])
        assert loc.netloc == "mothership.test"
        assert loc.path == "/sso/leo/my-box"
        q = parse_qs(loc.query)
        assert q["retry"] == ["1"]
        assert q["prompt"] == ["hi"]  # passthrough preserved

    def test_expired_with_retry_renders_login_page_no_bounce(self, db_engine, client):
        fake = FakeMothership(error_code="grant_expired")
        with _install_mothership(fake):
            resp = client.get(
                "/auth/consume?token=OLD&retry=1", follow_redirects=False
            )
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # Loop broken: manual recovery link (no retry param) present, named
        # after the brand domain in play (see app/services/sso_origin.py).
        assert "Continue with mothership.test" in resp.text
        assert any(r["error_class"] == "UnifiedLogin::ConsumeFailed" for r in fake.reported)

    def test_unreachable_renders_login_page_no_bounce(self, db_engine, client):
        fake = FakeMothership(error_code="mothership_unreachable")
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_no_token_redirects_to_login(self, db_engine, client):
        fake = FakeMothership(error_code="mothership_unreachable")
        with _install_mothership(fake):
            resp = client.get("/auth/consume", follow_redirects=False)
        assert resp.status_code == 302
        assert urlparse(resp.headers["location"]).path == "/login"


class TestFirstBoot:
    def test_zero_users_valid_token_provisions_admin(self, db_engine, client):
        assert _users(db_engine) == []  # empty box
        fake = FakeMothership(payload={
            "success": True,
            "user": {"guid": "guid-first", "email": "founder@e.com", "name": "F"},
            "role": "owner",
        })
        with _install_mothership(fake):
            resp = client.get("/auth/consume?token=T", follow_redirects=False)
        assert resp.status_code == 302
        users = _users(db_engine)
        assert len(users) == 1
        assert users[0].is_admin is True  # owner → admin
        assert users[0].llamapress_user_guid == "guid-first"


# =============================================================================
# Phase 3 companion — POST /internal/redeem_rails_grant (proxy for the gem).
#
# The gem's Rails container has NO mothership credentials, so it cannot call
# verify_login_grant directly. It POSTs {token} here; we redeem the SAME grant
# a second time with audience="rails_app" using OUR creds and relay the result.
# These mirror the gem's redeem_rails_grant_spec.rb contract from the box side.
# =============================================================================

# What the mothership returns for a rails_app redemption — note: NO link_username
# (that key is llamabot-only). The gem reads user.guid / role / permissions off
# this verbatim, so we must relay it unchanged with a 200.
RAILS_SUCCESS_BODY = {
    "success": True,
    "user": {"guid": "guid-abc-123", "email": "owner@example.com", "name": "Jane Doe"},
    "role": "owner",
    "permissions": ["chat", "rails_app"],
}


class RecordingMothership:
    """Fake that records the (token, audience) it was called with."""

    def __init__(self, *, payload=None, error_code=None):
        self._payload = payload
        self._error = error_code
        self.enabled = True
        self.calls = []

    async def verify_login_grant(self, token, audience="llamabot"):
        self.calls.append((token, audience))
        return self._payload, self._error


class TestRedeemRailsGrant:
    def test_success_relays_payload_verbatim_with_rails_app_audience(self, client):
        fake = RecordingMothership(payload=RAILS_SUCCESS_BODY)
        with _install_mothership(fake):
            resp = client.post(
                "/internal/redeem_rails_grant", json={"token": "raw"}
            )
        assert resp.status_code == 200
        # Redeemed against the rails_app audience (the whole point of Phase 3).
        assert fake.calls == [("raw", "rails_app")]
        # Relayed verbatim — the gem digs user.guid / role / permissions off this.
        assert resp.json() == RAILS_SUCCESS_BODY

    def test_business_failure_returns_error_code_non_200(self, client):
        fake = RecordingMothership(error_code="grant_expired")
        with _install_mothership(fake):
            resp = client.post(
                "/internal/redeem_rails_grant", json={"token": "stale"}
            )
        assert resp.status_code == 410  # gem only reads error_code, but map it
        body = resp.json()
        assert body["success"] is False
        assert body["error_code"] == "grant_expired"

    def test_grant_used_and_not_found_map_to_their_statuses(self, client):
        for code, status in [("grant_used", 409), ("grant_not_found", 404),
                             ("bad_audience", 422)]:
            fake = RecordingMothership(error_code=code)
            with _install_mothership(fake):
                resp = client.post(
                    "/internal/redeem_rails_grant", json={"token": "x"}
                )
            assert resp.status_code == status
            assert resp.json()["error_code"] == code

    def test_unreachable_maps_to_502(self, client):
        fake = RecordingMothership(error_code="mothership_unreachable")
        with _install_mothership(fake):
            resp = client.post(
                "/internal/redeem_rails_grant", json={"token": "x"}
            )
        assert resp.status_code == 502
        assert resp.json()["error_code"] == "mothership_unreachable"

    def test_missing_token_returns_400(self, client):
        fake = RecordingMothership(payload=RAILS_SUCCESS_BODY)
        with _install_mothership(fake):
            resp = client.post("/internal/redeem_rails_grant", json={})
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "missing_token"
        # Never touched the mothership for an empty token.
        assert fake.calls == []
