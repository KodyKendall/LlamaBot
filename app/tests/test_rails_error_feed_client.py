"""The HTTP half of mid-turn Rails auto-recovery.

Driven entirely by ``httpx.MockTransport`` — no network, no Rails. The single
behaviour that matters most here is the one repeated in almost every test: when
anything at all goes wrong, ``fetch`` returns None and the turn carries on. This
runs on every model call in the product; it is not allowed to be the reason a
turn fails.

See docs/dev/rails_auto_recovery.md.
"""
import httpx
import pytest

from app.services.rails_error_feed import RailsErrorFeedClient


def client_with(handler, token="tok_abc"):
    transport = httpx.MockTransport(handler)
    return RailsErrorFeedClient(
        base_url="http://llamapress:3000",
        token=token,
        client_factory=lambda timeout: httpx.AsyncClient(
            transport=transport, timeout=timeout
        ),
    )


def json_response(payload, status=200):
    return lambda request: httpx.Response(status, json=payload)


@pytest.mark.asyncio
class TestHappyPath:
    async def test_returns_the_cursor_and_the_entries(self):
        feed = client_with(json_response({
            "seq": 42,
            "errors": [{"seq": 42, "fingerprint": "f", "error_class": "TypeError"}],
        }))

        result = await feed.fetch(since=41)

        assert result is not None
        seq, errors = result
        assert seq == 42
        assert errors[0]["error_class"] == "TypeError"

    async def test_sends_the_box_feed_credential(self, monkeypatch):
        # On a real box SECRET_KEY_BASE is always set, so this is the header
        # Rails actually sees. The per-user fallback is pinned separately in
        # TestCredentialChoice.
        monkeypatch.setenv("SECRET_KEY_BASE", "s3cret")
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"seq": 0, "errors": []})

        await client_with(handler).fetch(since=0)

        assert seen["auth"].startswith("LlamaBotFeed ")

    async def test_hits_the_engine_endpoint(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"seq": 0, "errors": []})

        await client_with(handler).fetch(since=0)

        assert seen["url"].startswith("http://llamapress:3000/llama_bot/errors")

    async def test_a_probe_sends_no_since_param(self):
        # Priming the turn's cursor: ask where the log is, take nothing.
        seen = {}

        def handler(request):
            seen["query"] = request.url.params
            return httpx.Response(200, json={"seq": 7, "errors": []})

        result = await client_with(handler).fetch(since=None)

        assert "since" not in seen["query"]
        assert result == (7, [])

    async def test_a_delta_fetch_sends_the_cursor(self):
        seen = {}

        def handler(request):
            seen["since"] = request.url.params.get("since")
            return httpx.Response(200, json={"seq": 9, "errors": []})

        await client_with(handler).fetch(since=7)

        assert seen["since"] == "7"


@pytest.mark.asyncio
class TestDegradesQuietly:
    async def test_forbidden_returns_none(self):
        # The per-user Rails token has a 30-minute TTL; an expired one must cost
        # the feature, not the turn.
        assert await client_with(json_response({}, status=403)).fetch(since=0) is None

    async def test_not_found_returns_none(self):
        # A box running a gem older than 0.7.4 has no such endpoint.
        assert await client_with(json_response({}, status=404)).fetch(since=0) is None

    async def test_server_error_returns_none(self):
        assert await client_with(json_response({}, status=500)).fetch(since=0) is None

    async def test_a_timeout_returns_none(self):
        def handler(request):
            raise httpx.ConnectTimeout("too slow")

        assert await client_with(handler).fetch(since=0) is None

    async def test_a_connection_error_returns_none(self):
        def handler(request):
            raise httpx.ConnectError("rails is restarting")

        assert await client_with(handler).fetch(since=0) is None

    async def test_malformed_json_returns_none(self):
        def handler(request):
            return httpx.Response(200, content=b"<html>not json</html>")

        assert await client_with(handler).fetch(since=0) is None

    async def test_a_body_without_a_cursor_returns_none(self):
        assert await client_with(json_response({"errors": []})).fetch(since=0) is None

    async def test_a_body_whose_errors_are_not_a_list_returns_none(self):
        payload = {"seq": 3, "errors": "everything is fine"}
        assert await client_with(json_response(payload)).fetch(since=0) is None

    async def test_no_credential_of_any_kind_means_no_request(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY_BASE", raising=False)
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"seq": 0, "errors": []})

        assert await client_with(handler, token=None).fetch(since=0) is None
        assert calls == []


@pytest.mark.asyncio
class TestArmingWindow:
    """`within` is how a turn arms when the app is ALREADY broken.

    A seq cursor alone is silent in that case: the user is parked on the error
    page, so no NEW request happens and nothing ever lands after the cursor.
    """

    async def test_a_window_probe_sends_within_and_no_since(self):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"seq": 4, "errors": [{"seq": 4}]})

        result = await client_with(handler).fetch(since=None, within=120)

        assert seen["params"] == {"within": "120"}
        assert result[0] == 4

    async def test_a_cursor_fetch_ignores_the_window(self):
        # Once armed, the cursor is authoritative; sending both would re-deliver
        # errors the turn has already been told about.
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"seq": 9, "errors": []})

        await client_with(handler).fetch(since=7, within=120)

        assert seen["params"] == {"since": "7"}


class TestBoxFeedToken:
    """The credential that made this feature actually work.

    v1 authenticated as the signed-in Rails user, using the `api_token` the chat
    frontend mints. That token only exists while the human holds a Devise
    session in the browser tab, so on 2026-08-24 the whole feature silently
    no-opped with `no Rails api_token on the frame`. The box reading its own
    error log must not depend on a browser session.
    """

    def test_it_derives_the_token_from_the_shared_box_secret(self, monkeypatch):
        from app.services.rails_error_feed import feed_token

        monkeypatch.setenv("SECRET_KEY_BASE", "s3cret")
        assert feed_token() == feed_token()
        assert len(feed_token()) == 64

    def test_a_different_secret_derives_a_different_token(self, monkeypatch):
        from app.services.rails_error_feed import feed_token

        monkeypatch.setenv("SECRET_KEY_BASE", "one")
        first = feed_token()
        monkeypatch.setenv("SECRET_KEY_BASE", "two")
        assert feed_token() != first

    def test_no_secret_means_no_token(self, monkeypatch):
        from app.services.rails_error_feed import feed_token

        monkeypatch.delenv("SECRET_KEY_BASE", raising=False)
        assert feed_token() is None

    def test_a_blank_secret_means_no_token(self, monkeypatch):
        from app.services.rails_error_feed import feed_token

        monkeypatch.setenv("SECRET_KEY_BASE", "   ")
        assert feed_token() is None

    def test_it_matches_the_ruby_side_byte_for_byte(self, monkeypatch):
        # Pinned against OpenSSL::HMAC.hexdigest("SHA256", secret, purpose) —
        # the two implementations are in different languages and nothing else
        # would catch them drifting apart.
        import hashlib
        import hmac as _hmac

        from app.services.rails_error_feed import feed_token

        monkeypatch.setenv("SECRET_KEY_BASE", "s3cret")
        expected = _hmac.new(b"s3cret", b"llamabot-error-feed", hashlib.sha256).hexdigest()
        assert feed_token() == expected


@pytest.mark.asyncio
class TestCredentialChoice:
    async def test_it_prefers_the_box_token_over_the_user_token(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY_BASE", "s3cret")
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"seq": 0, "errors": []})

        await client_with(handler, token="user-token").fetch(since=0)

        assert seen["auth"].startswith("LlamaBotFeed ")

    async def test_it_works_with_no_user_token_at_all(self, monkeypatch):
        # The exact case that was broken: nobody signed into Rails.
        monkeypatch.setenv("SECRET_KEY_BASE", "s3cret")

        def handler(request):
            return httpx.Response(200, json={"seq": 3, "errors": []})

        assert await client_with(handler, token=None).fetch(since=0) == (3, [])

    async def test_it_falls_back_to_the_user_token_without_a_box_secret(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY_BASE", raising=False)
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"seq": 0, "errors": []})

        await client_with(handler, token="user-token").fetch(since=0)

        assert seen["auth"] == "LlamaBot user-token"

    async def test_no_credential_at_all_makes_no_request(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY_BASE", raising=False)
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"seq": 0, "errors": []})

        assert await client_with(handler, token=None).fetch(since=0) is None
        assert calls == []
