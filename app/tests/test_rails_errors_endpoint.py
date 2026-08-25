"""The /api/rails-errors proxy the chat page's error tray polls.

Why a proxy at all, rather than the browser reading the Rails feed directly:
``GET /llama_bot/errors`` sets no CORS headers and the chat page is a different
origin, so a direct fetch is blocked. Proxying also means this shipped in the
LlamaBot image alone — it works against every gem old enough to serve the feed,
with no skeleton release in the way.

What matters here: the Rails bearer token travels in a header and never in the
query string (it is a live 30-minute credential and query strings land in access
logs), a cursor probe stays a probe, and every failure degrades to "no
information" instead of an error the tray would have to render.
"""

import asyncio
from typing import Optional

import pytest

from app.routers import api


class FakeRequest:
    def __init__(self, params=None, headers=None):
        self.query_params = params or {}
        self.headers = headers or {}


class FakeFeed:
    """Stands in for RailsErrorFeedClient."""

    last: Optional["FakeFeed"] = None

    def __init__(self, *, token=None, result=None, boom=False):
        self.token = token
        self._result = result
        self._boom = boom
        self.since = "unset"
        FakeFeed.last = self

    async def fetch(self, *, since):
        self.since = since
        if self._boom:
            raise RuntimeError("rails is restarting")
        return self._result


@pytest.fixture(autouse=True)
def reset_feed():
    FakeFeed.last = None
    yield
    FakeFeed.last = None


def call(request, **feed_kwargs):
    """Drive the helper the route delegates to.

    The route itself is two lines — an auth dependency and this call — because a
    ``feed_factory`` keyword on the handler would be read by FastAPI as a query
    parameter. ``test_endpoint_requires_a_signed_in_user`` covers the wiring.
    """
    def factory(token):
        return FakeFeed(token=token, **feed_kwargs)

    return asyncio.run(api._rails_errors(request, feed_factory=factory))


TOKEN = {"X-Rails-Api-Token": "tok-abc"}


def test_returns_shaped_entries_for_the_tray():
    body = call(
        FakeRequest({"since": "4"}, TOKEN),
        result=(6, [{
            "seq": 5,
            "error_class": "NoMethodError",
            "message": "undefined method `title' for nil",
            "method": "GET",
            "path": "/posts/1",
            "count": 1,
            "backtrace": ["app/views/posts/show.html.erb:3"],
        }]),
    )

    assert body["seq"] == 6
    assert body["available"] is True
    assert body["errors"] == [{
        "id": "rails-5",
        "kind": "rails",
        "message": "NoMethodError: undefined method `title' for nil",
        "path": "GET /posts/1",
        "count": 1,
        "stack": "app/views/posts/show.html.erb:3",
    }]


def test_token_comes_from_the_header():
    call(FakeRequest({"since": "4"}, TOKEN), result=(1, []))
    assert FakeFeed.last.token == "tok-abc"


def test_token_in_the_query_string_is_ignored():
    # A live bearer credential must not be loggable. If it only arrives this way
    # we behave as if we had no token at all.
    body = call(FakeRequest({"since": "4", "api_token": "tok-abc"}, {}))
    assert body == {"seq": None, "errors": [], "available": False}
    assert FakeFeed.last is None


def test_no_token_never_dials_rails():
    body = call(FakeRequest({"since": "4"}, {}))
    assert body["available"] is False
    assert FakeFeed.last is None


def test_missing_since_is_a_cursor_probe():
    # How the tray arms itself on page load: learn where the log stands, take
    # nothing. Crashes from before the user opened the page are not news.
    body = call(FakeRequest({}, TOKEN), result=(9, []))
    assert FakeFeed.last.since is None
    assert body == {"seq": 9, "errors": [], "available": True}


def test_garbled_cursor_is_a_probe_not_a_replay():
    # Reading a junk cursor as 0 would replay the whole ring into the tray.
    call(FakeRequest({"since": "not-a-number"}, TOKEN), result=(9, []))
    assert FakeFeed.last.since is None


def test_negative_cursor_is_a_probe():
    call(FakeRequest({"since": "-3"}, TOKEN), result=(9, []))
    assert FakeFeed.last.since is None


def test_cursor_is_passed_through_as_an_int():
    call(FakeRequest({"since": "12"}, TOKEN), result=(12, []))
    assert FakeFeed.last.since == 12


def test_unavailable_feed_reports_no_information():
    # Gem too old to serve the endpoint, token expired, Rails restarting. The
    # tray holds its cursor and tries again; it must not render an error about
    # not being able to fetch errors.
    body = call(FakeRequest({"since": "4"}, TOKEN), result=None)
    assert body == {"seq": None, "errors": [], "available": False}


def test_a_raising_feed_is_not_a_500():
    body = call(FakeRequest({"since": "4"}, TOKEN), boom=True)
    assert body == {"seq": None, "errors": [], "available": False}


def test_endpoint_requires_a_signed_in_user():
    # Backtraces name file paths and line numbers; this is not public.
    from app.dependencies import auth

    route = next(r for r in api.router.routes if getattr(r, "path", None) == "/api/rails-errors")
    assert any(d.call is auth for d in route.dependant.dependencies)
