"""Building-overlay promo slot ("jumbotron").

The creative is mothership-owned, so the properties worth pinning here are the
defensive ones: a remote payload can't hand the browser an unbounded number of
snippets, an oversized snippet, a silly slot height or a 1-second rotation; and
every failure mode (no mothership, old mothership, timeout, garbage JSON)
degrades to "no promos" rather than a broken overlay.

Run with: pytest app/tests/test_overlay_ads.py -v
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import overlay_ads


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No cache carryover, and no dependence on the box's real .leonardo/.

    The override file wins over the mothership by design, so a QA/preview
    `overlay_ads.json` sitting on the dev box would silently hijack every
    endpoint test below. Point the path somewhere that cannot exist; the tests
    that DO exercise the override re-patch it to a tmp_path.
    """
    monkeypatch.setattr(overlay_ads, "LOCAL_OVERRIDE_PATH", "/nonexistent/overlay_ads.json")
    overlay_ads.clear_cache()
    yield
    overlay_ads.clear_cache()


@pytest.fixture
def client():
    from app.routers.api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _mothership(enabled=True, payload=None):
    """Patch MothershipClient as seen from inside the endpoint."""
    fake = AsyncMock()
    fake.enabled = enabled
    fake.fetch_overlay_ads = AsyncMock(return_value=payload)
    return patch("app.services.mothership_client.MothershipClient", return_value=fake), fake


# --------------------------------------------------------------------------
# normalize()
# --------------------------------------------------------------------------

def test_normalize_keeps_good_ads_and_fills_defaults():
    out = overlay_ads.normalize({"ads": [{"id": "spring", "html": "<b>hi</b>"}]})

    assert out["ads"] == [
        {"id": "spring", "html": "<b>hi</b>", "height": overlay_ads.DEFAULT_AD_HEIGHT}
    ]
    assert out["rotate_seconds"] == overlay_ads.DEFAULT_ROTATE_SECONDS


def test_normalize_drops_bad_entries_without_dropping_good_ones():
    out = overlay_ads.normalize({"ads": [
        {"html": "   "},              # blank
        {"id": "no-html"},            # missing html
        "not-a-dict",
        {"id": "keep", "html": "<p>ok</p>"},
    ]})

    assert [a["id"] for a in out["ads"]] == ["keep"]


def test_normalize_drops_oversized_snippet_rather_than_truncating():
    big = "x" * (overlay_ads.MAX_AD_BYTES + 1)
    out = overlay_ads.normalize({"ads": [{"id": "huge", "html": big}]})

    assert out["ads"] == []


def test_normalize_caps_the_number_of_ads():
    many = [{"id": f"a{i}", "html": "<b>x</b>"} for i in range(overlay_ads.MAX_ADS + 5)]
    out = overlay_ads.normalize({"ads": many})

    assert len(out["ads"]) == overlay_ads.MAX_ADS


@pytest.mark.parametrize("sent,expected", [
    (1, overlay_ads.MIN_ROTATE_SECONDS),
    (999999, overlay_ads.MAX_ROTATE_SECONDS),
    ("banana", overlay_ads.DEFAULT_ROTATE_SECONDS),
    (None, overlay_ads.DEFAULT_ROTATE_SECONDS),
    (180, 180),
])
def test_normalize_clamps_rotation(sent, expected):
    out = overlay_ads.normalize({"ads": [], "rotate_seconds": sent})
    assert out["rotate_seconds"] == expected


@pytest.mark.parametrize("sent,expected", [
    (5, overlay_ads.MIN_AD_HEIGHT),
    (5000, overlay_ads.MAX_AD_HEIGHT),
    ("tall", overlay_ads.DEFAULT_AD_HEIGHT),
])
def test_normalize_clamps_height(sent, expected):
    out = overlay_ads.normalize({"ads": [{"id": "a", "html": "<b>x</b>", "height": sent}]})
    assert out["ads"][0]["height"] == expected


@pytest.mark.parametrize("payload", [None, "nope", {"ads": "not-a-list"}, {}])
def test_normalize_never_raises_on_garbage(payload):
    assert overlay_ads.normalize(payload) == overlay_ads.EMPTY


# --------------------------------------------------------------------------
# GET /api/overlay-ads
# --------------------------------------------------------------------------

def test_endpoint_returns_empty_when_mothership_not_configured(client):
    patcher, fake = _mothership(enabled=False)
    with patcher:
        body = client.get("/api/overlay-ads").json()

    assert body == overlay_ads.EMPTY
    fake.fetch_overlay_ads.assert_not_awaited()


def test_endpoint_returns_empty_when_mothership_call_fails(client):
    # fetch_overlay_ads swallows its own errors and returns None (old mothership,
    # timeout, 404). The endpoint must not turn that into a 500.
    patcher, _ = _mothership(payload=None)
    with patcher:
        response = client.get("/api/overlay-ads")

    assert response.status_code == 200
    assert response.json() == overlay_ads.EMPTY


def test_endpoint_serves_normalized_ads(client):
    patcher, _ = _mothership(payload={
        "ads": [{"id": "promo", "html": "<h1>Try Pro</h1>", "height": 5000}],
        "rotate_seconds": 1,
    })
    with patcher:
        body = client.get("/api/overlay-ads").json()

    assert body["ads"][0]["html"] == "<h1>Try Pro</h1>"
    assert body["ads"][0]["height"] == overlay_ads.MAX_AD_HEIGHT
    assert body["rotate_seconds"] == overlay_ads.MIN_ROTATE_SECONDS


def test_second_request_is_served_from_cache(client):
    patcher, fake = _mothership(payload={"ads": [{"id": "p", "html": "<b>x</b>"}]})
    with patcher:
        first = client.get("/api/overlay-ads").json()
        second = client.get("/api/overlay-ads").json()

    assert first == second
    assert fake.fetch_overlay_ads.await_count == 1


def test_a_failed_fetch_is_also_cached(client):
    """An unreachable mothership must not be re-dialed on every build."""
    patcher, fake = _mothership(payload=None)
    with patcher:
        client.get("/api/overlay-ads")
        client.get("/api/overlay-ads")

    assert fake.fetch_overlay_ads.await_count == 1


# --------------------------------------------------------------------------
# .leonardo/overlay_ads.json override
# --------------------------------------------------------------------------

def test_local_override_wins_over_the_mothership(client, tmp_path, monkeypatch):
    override = tmp_path / "overlay_ads.json"
    override.write_text('{"ads": [{"id": "local", "html": "<b>local</b>"}]}')
    monkeypatch.setattr(overlay_ads, "LOCAL_OVERRIDE_PATH", str(override))

    patcher, fake = _mothership(payload={"ads": [{"id": "remote", "html": "<b>remote</b>"}]})
    with patcher:
        body = client.get("/api/overlay-ads").json()

    assert [a["id"] for a in body["ads"]] == ["local"]
    fake.fetch_overlay_ads.assert_not_awaited()


def test_local_override_is_re_read_every_request(client, tmp_path, monkeypatch):
    """Editing the file and reloading must show the new promo — no cache in the way."""
    override = tmp_path / "overlay_ads.json"
    override.write_text('{"ads": [{"id": "v1", "html": "<b>v1</b>"}]}')
    monkeypatch.setattr(overlay_ads, "LOCAL_OVERRIDE_PATH", str(override))

    patcher, _ = _mothership(payload=None)
    with patcher:
        assert client.get("/api/overlay-ads").json()["ads"][0]["id"] == "v1"
        override.write_text('{"ads": [{"id": "v2", "html": "<b>v2</b>"}]}')
        assert client.get("/api/overlay-ads").json()["ads"][0]["id"] == "v2"


def test_unreadable_override_falls_back_to_the_mothership(client, tmp_path, monkeypatch):
    override = tmp_path / "overlay_ads.json"
    override.write_text("{ this is not json")
    monkeypatch.setattr(overlay_ads, "LOCAL_OVERRIDE_PATH", str(override))

    patcher, _ = _mothership(payload={"ads": [{"id": "remote", "html": "<b>remote</b>"}]})
    with patcher:
        body = client.get("/api/overlay-ads").json()

    assert [a["id"] for a in body["ads"]] == ["remote"]


def test_missing_override_file_is_not_an_error(client, monkeypatch):
    monkeypatch.setattr(overlay_ads, "LOCAL_OVERRIDE_PATH", "/nope/does/not/exist.json")

    patcher, _ = _mothership(payload={"ads": [{"id": "remote", "html": "<b>remote</b>"}]})
    with patcher:
        assert client.get("/api/overlay-ads").status_code == 200


# --------------------------------------------------------------------------
# Display policy — WHEN the jumbotron shows
#
# The whole point is that the mothership decides, so these pin two things: that
# a partial policy overrides only what it names, and that no policy value can
# push the instance outside its clamps or override the never-during-a-question
# guarantee.
# --------------------------------------------------------------------------

def test_missing_policy_gives_the_baked_in_defaults():
    out = overlay_ads.normalize({"ads": []})
    assert out["policy"] == overlay_ads.DEFAULT_POLICY


@pytest.mark.parametrize("raw", [None, "nope", 42, []])
def test_unusable_policy_gives_the_defaults(raw):
    assert overlay_ads.normalize_policy(raw) == overlay_ads.DEFAULT_POLICY


def test_policy_fields_override_independently():
    """Sending one field must not reset the others to defaults."""
    policy = overlay_ads.normalize_policy({"show_after_seconds": 20})

    assert policy["show_after_seconds"] == 20
    assert policy["enabled"] is overlay_ads.DEFAULT_POLICY["enabled"]
    assert policy["modes"] == overlay_ads.DEFAULT_POLICY["modes"]
    assert policy["min_interval_seconds"] == overlay_ads.DEFAULT_POLICY["min_interval_seconds"]


def test_policy_can_switch_the_whole_feature_off():
    assert overlay_ads.normalize_policy({"enabled": False})["enabled"] is False


@pytest.mark.parametrize("sent,expected", [
    (-5, 0),
    (99999, overlay_ads.MAX_SHOW_AFTER_SECONDS),
    ("soon", overlay_ads.DEFAULT_SHOW_AFTER_SECONDS),
    (0, 0),
    (30, 30),
])
def test_show_after_seconds_is_clamped(sent, expected):
    assert overlay_ads.normalize_policy({"show_after_seconds": sent})["show_after_seconds"] == expected


@pytest.mark.parametrize("sent,expected", [
    (-1, 0),
    (10 ** 9, overlay_ads.MAX_MIN_INTERVAL_SECONDS),
    ("often", overlay_ads.DEFAULT_MIN_INTERVAL_SECONDS),
    (1800, 1800),
])
def test_min_interval_seconds_is_clamped(sent, expected):
    got = overlay_ads.normalize_policy({"min_interval_seconds": sent})["min_interval_seconds"]
    assert got == expected


def test_modes_can_be_narrowed_to_one_layout():
    assert overlay_ads.normalize_policy({"modes": ["building"]})["modes"] == ["building"]


def test_question_can_never_be_selected_as_a_mode():
    """Leo is blocked on the user during a question; nothing competes with that.

    This is the one guarantee the mothership cannot buy its way out of, so it is
    dropped even when explicitly requested.
    """
    policy = overlay_ads.normalize_policy({"modes": ["building", "plan", "question"]})
    assert policy["modes"] == ["building", "plan"]
    assert "question" not in policy["modes"]


def test_unknown_modes_are_dropped_not_passed_through():
    assert overlay_ads.normalize_policy({"modes": ["building", "jumbotron"]})["modes"] == ["building"]


def test_empty_modes_list_means_never_show():
    assert overlay_ads.normalize_policy({"modes": []})["modes"] == []


def test_variant_is_echoed_for_experiment_attribution():
    out = overlay_ads.normalize({"ads": [], "variant": "exp-delay-8s"})
    assert out["variant"] == "exp-delay-8s"


def test_endpoint_serves_the_policy(client):
    patcher, _ = _mothership(payload={
        "ads": [{"id": "p", "html": "<b>x</b>"}],
        "policy": {"show_after_seconds": 12, "modes": ["building"]},
        "variant": "arm-b",
    })
    with patcher:
        body = client.get("/api/overlay-ads").json()

    assert body["policy"]["show_after_seconds"] == 12
    assert body["policy"]["modes"] == ["building"]
    assert body["variant"] == "arm-b"


def test_empty_payload_still_carries_a_usable_policy():
    """The overlay reads policy unconditionally; it must never be absent."""
    assert overlay_ads.empty()["policy"] == overlay_ads.DEFAULT_POLICY


def test_empty_returns_a_fresh_dict_each_time():
    """Callers mutate what they get back; a shared constant would leak between them."""
    a = overlay_ads.empty()
    a["policy"]["enabled"] = False
    assert overlay_ads.empty()["policy"]["enabled"] is True


# --------------------------------------------------------------------------
# Personalisation — the mothership targets per user, so the cache must too
# --------------------------------------------------------------------------

def _as_user(user):
    """Patch the session-cookie resolver the endpoint uses."""
    return patch("app.dependencies._user_from_session_cookie", return_value=user)


class _FakeUser:
    def __init__(self, id, username, role="user", is_admin=False,
                 email=None, llamapress_user_guid=None):
        self.id, self.username, self.role, self.is_admin = id, username, role, is_admin
        self.email, self.llamapress_user_guid = email, llamapress_user_guid


def test_user_context_is_sent_to_the_mothership(client):
    """The same wire shape the mothership reports use (user_context.describe),
    so a promo can be targeted by the same guid the telemetry is keyed on."""
    patcher, fake = _mothership(payload={"ads": []})
    user = _FakeUser(7, "kody", role="engineer", is_admin=True,
                     email="kody@llamapress.ai", llamapress_user_guid="guid-7")
    with patcher, _as_user(user):
        client.get("/api/overlay-ads")

    sent = fake.fetch_overlay_ads.await_args.kwargs["user"]
    assert sent == {
        "id": 7,
        "username": "kody",
        "email": "kody@llamapress.ai",
        "llamapress_user_guid": "guid-7",
        "role": "engineer",
        "is_admin": True,
    }


def test_anonymous_request_sends_no_user(client):
    patcher, fake = _mothership(payload={"ads": []})
    with patcher, _as_user(None):
        client.get("/api/overlay-ads")

    assert fake.fetch_overlay_ads.await_args.kwargs["user"] is None


def test_one_users_personalised_ads_do_not_leak_to_another(client):
    """The cache is keyed per user. A shared cache would hand the first
    requester's targeted promos and policy to everyone else on the box."""
    calls = []

    async def _per_user(version, user=None):
        calls.append(user["id"])
        return {"ads": [{"id": f"for-user-{user['id']}", "html": "<b>x</b>"}]}

    fake = AsyncMock()
    fake.enabled = True
    fake.fetch_overlay_ads = _per_user

    with patch("app.services.mothership_client.MothershipClient", return_value=fake):
        with _as_user(_FakeUser(1, "alice")):
            first = client.get("/api/overlay-ads").json()
        with _as_user(_FakeUser(2, "bob")):
            second = client.get("/api/overlay-ads").json()

    assert first["ads"][0]["id"] == "for-user-1"
    assert second["ads"][0]["id"] == "for-user-2"
    assert calls == [1, 2], "each user must get their own fetch, not a cache hit"


def test_the_same_user_still_gets_a_cache_hit(client):
    patcher, fake = _mothership(payload={"ads": [{"id": "p", "html": "<b>x</b>"}]})
    with patcher, _as_user(_FakeUser(1, "alice")):
        client.get("/api/overlay-ads")
        client.get("/api/overlay-ads")

    assert fake.fetch_overlay_ads.await_count == 1


def test_endpoint_survives_a_dead_user_lookup(client):
    """A DB hiccup must not 500 an endpoint whose contract is to fail open."""
    patcher, _ = _mothership(payload={"ads": [{"id": "p", "html": "<b>x</b>"}]})
    with patcher, patch("app.dependencies._user_from_session_cookie", side_effect=RuntimeError("db down")):
        response = client.get("/api/overlay-ads")

    assert response.status_code == 200
    assert response.json()["ads"][0]["id"] == "p"
