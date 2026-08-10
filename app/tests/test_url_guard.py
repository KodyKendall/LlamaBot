"""Tests for the outbound URL guard that fronts every server-side browser navigation.

Background: a security researcher reported (2026-08-06) that the page-clone tool
passed an LLM-supplied URL straight to Playwright's ``page.goto()`` with no
validation, and used it to read the instance's own ``/api/available-models``
endpoint from inside the container. The clone tool is gone; ``browser_inspect``
is the remaining server-side navigation surface and is guarded here.

The guard is an ALLOWLIST, not a denylist: ``browser_inspect`` exists to inspect
the box's own Rails app at ``http://llamapress:3000``, so a blanket "block all
private IPs" rule would break its actual purpose. Internal traffic is permitted
only to the Rails origin; everything else must resolve to a public address.

DNS is mocked throughout so these tests never touch the network.
"""
import socket
from unittest.mock import MagicMock, patch

import pytest

from app.agents.utils.url_guard import (
    UrlNotAllowed,
    allowed_internal_origins,
    validate_outbound_url,
)


def _fake_getaddrinfo(*ips):
    """Build a getaddrinfo stub resolving any host to the given literal IPs."""
    def _resolve(host, port, *args, **kwargs):
        out = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port or 0, 0, 0) if family == socket.AF_INET6 else (ip, port or 0)
            out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return out
    return _resolve


class TestSchemeValidation:
    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "gopher://127.0.0.1:6379/_INFO",
        "ftp://internal/backup.tar",
        "data:text/html,<script>alert(1)</script>",
        "about:blank",
        "javascript:fetch('/api/available-models')",
    ])
    def test_non_http_schemes_rejected(self, url):
        with pytest.raises(UrlNotAllowed):
            validate_outbound_url(url)

    def test_missing_hostname_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_outbound_url("http:///no-host")


class TestInternalAddressesBlocked:
    def test_blocks_the_reported_repro_llamabot_own_api(self):
        """The exact URL from the disclosure: the instance reading its own API."""
        with pytest.raises(UrlNotAllowed):
            validate_outbound_url("http://localhost:8000/api/available-models")

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8000/api/available-models",
        "http://[::1]:8000/api/available-models",
        "http://0.0.0.0:8000/",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.0.0.5:6379/",
        "http://172.16.4.4:5432/",
        "http://192.168.1.1/admin",
        "http://2130706433/",          # decimal-encoded 127.0.0.1
        "http://0x7f000001/",          # hex-encoded 127.0.0.1
    ])
    def test_blocks_internal_destinations(self, url):
        # metadata.google.internal is the only name here needing resolution.
        with patch("socket.getaddrinfo", _fake_getaddrinfo("169.254.169.254")):
            with pytest.raises(UrlNotAllowed):
                validate_outbound_url(url)

    def test_blocks_public_hostname_resolving_to_loopback(self):
        """A hostname the attacker controls that points at 127.0.0.1."""
        with patch("socket.getaddrinfo", _fake_getaddrinfo("127.0.0.1")):
            with pytest.raises(UrlNotAllowed):
                validate_outbound_url("http://evil.example.com/")

    def test_blocks_when_any_resolved_address_is_internal(self):
        """Multi-record DNS: one public A record does not launder a private one."""
        with patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34", "10.1.2.3")):
            with pytest.raises(UrlNotAllowed):
                validate_outbound_url("http://mixed.example.com/")

    def test_blocks_ipv4_mapped_ipv6_loopback(self):
        with patch("socket.getaddrinfo", _fake_getaddrinfo("::ffff:127.0.0.1")):
            with pytest.raises(UrlNotAllowed):
                validate_outbound_url("http://sneaky.example.com/")

    def test_blocks_unresolvable_host(self):
        def _boom(*a, **k):
            raise socket.gaierror("nope")
        with patch("socket.getaddrinfo", _boom):
            with pytest.raises(UrlNotAllowed):
                validate_outbound_url("http://does-not-exist.example/")


class TestAllowedDestinations:
    def test_allows_public_address(self):
        with patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")):
            validate_outbound_url("https://example.com/page")

    def test_allows_the_rails_app_internal_origin(self):
        """browser_inspect's documented primary use — must keep working."""
        validate_outbound_url("http://llamapress:3000/dashboard")

    def test_allows_rails_app_on_loopback(self):
        validate_outbound_url("http://localhost:3000/todos")

    def test_allowlist_is_port_scoped_not_host_scoped(self):
        """Allowing the Rails app must not allow every other port on that host."""
        with pytest.raises(UrlNotAllowed):
            validate_outbound_url("http://llamapress:5432/")

    def test_operator_can_extend_allowlist_via_env(self):
        with patch.dict("os.environ", {"BROWSER_ALLOWED_ORIGINS": "http://staging-box:4000"}):
            validate_outbound_url("http://staging-box:4000/health")

    def test_rails_base_url_env_drives_the_default_allowlist(self):
        with patch.dict("os.environ", {"RAILS_BASE_URL": "http://myapp:3001"}):
            assert "myapp:3001" in allowed_internal_origins()


class TestGuardedRouteHandler:
    """Redirects and subresources are re-checked, not just the initial URL."""

    def _route_for(self, url):
        route = MagicMock()
        route.request.url = url
        return route

    def test_aborts_request_to_internal_address(self):
        from app.agents.utils.url_guard import guarded_route_handler
        route = self._route_for("http://localhost:8000/api/available-models")
        guarded_route_handler()(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()

    def test_allows_request_to_the_rails_app(self):
        from app.agents.utils.url_guard import guarded_route_handler
        route = self._route_for("http://llamapress:3000/assets/app.css")
        guarded_route_handler()(route)
        route.continue_.assert_called_once()
        route.abort.assert_not_called()

    def test_allows_public_subresource(self):
        from app.agents.utils.url_guard import guarded_route_handler
        route = self._route_for("https://cdn.example.com/lib.js")
        with patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")):
            guarded_route_handler()(route)
        route.continue_.assert_called_once()

    def test_blocks_redirect_landing_on_metadata_endpoint(self):
        """A public URL that 302s to the metadata service must not follow through."""
        from app.agents.utils.url_guard import guarded_route_handler
        handler = guarded_route_handler()
        first = self._route_for("https://totally-fine.example.com/start")
        with patch("socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")):
            handler(first)
        first.continue_.assert_called_once()

        redirected = self._route_for("http://169.254.169.254/latest/meta-data/")
        handler(redirected)
        redirected.abort.assert_called_once()
        redirected.continue_.assert_not_called()

    def test_verdicts_are_cached_per_origin(self):
        """Repeat subresource fetches must not re-resolve DNS every time."""
        from app.agents.utils.url_guard import guarded_route_handler
        handler = guarded_route_handler()
        resolver = MagicMock(side_effect=_fake_getaddrinfo("93.184.216.34"))
        with patch("socket.getaddrinfo", resolver):
            for n in range(5):
                handler(self._route_for(f"https://cdn.example.com/asset-{n}.js"))
        assert resolver.call_count == 1


class TestBrowserInspectIntegration:
    """The guard must fire before Chromium is ever launched."""

    def _run(self, url):
        from app.agents.leonardo.rails_agent.tools import browser_inspect
        runtime = MagicMock()
        runtime.tool_call_id = "call_1"
        return browser_inspect.func(url=url, runtime=runtime)

    def test_blocked_url_never_launches_a_browser(self):
        with patch("playwright.sync_api.sync_playwright") as mock_pw:
            result = self._run("http://localhost:8000/api/available-models")
        mock_pw.assert_not_called()

        content = result.update["messages"][0].content
        assert "blocked" in content.lower()

    def test_blocked_url_returns_a_correctable_tool_message_not_an_exception(self):
        """The agent should get a usable error back, not a crashed turn."""
        with patch("playwright.sync_api.sync_playwright"):
            result = self._run("http://169.254.169.254/latest/meta-data/")
        msg = result.update["messages"][0]
        assert msg.tool_call_id == "call_1"
        assert "169.254.169.254" in msg.content or "blocked" in msg.content.lower()
