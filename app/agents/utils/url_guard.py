"""Guard for URLs the server fetches on an agent's behalf.

Any tool that makes the *server* open a URL chosen by the LLM is a server-side
request forgery (SSRF) surface: the model can be steered by untrusted content
(a page it was asked to read, a scraped result) into pointing the fetch at
things only the container can reach — the LlamaBot API itself, Postgres, Redis,
or a cloud metadata endpoint.

This is an ALLOWLIST, not a denylist. ``browser_inspect`` exists to load the
box's own Rails app at ``http://llamapress:3000``, so "block all private IPs"
would break the tool's actual job. Instead:

  * internal destinations are allowed only for the Rails app's own origin
    (host *and* port, so :3000 does not imply :5432), and
  * every other host must resolve exclusively to public addresses.

Residual risk, stated plainly: a host that passes validation and then re-resolves
to a private address on the real connection (DNS rebinding) is not fully closed
by name-based checks. Call ``guarded_route_handler`` alongside this so redirects
and subresources are re-checked at request time, and treat outbound network
isolation at the container level as the real backstop for a hostile model.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Iterable, Optional
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

# The Rails app the agent is building. Mirrors app/lib/llamapress_api.py.
DEFAULT_RAILS_BASE_URL = "http://llamapress:3000"

# Operator escape hatch: comma-separated origins ("http://staging:4000").
ALLOWED_ORIGINS_ENV = "BROWSER_ALLOWED_ORIGINS"


class UrlNotAllowed(ValueError):
    """Raised when the server may not fetch the given URL."""


def _origin(url: str) -> Optional[str]:
    """Return ``host:port`` for a URL, with the scheme's default port applied."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{host.lower()}:{port}"


def allowed_internal_origins() -> frozenset[str]:
    """Internal ``host:port`` origins the server is permitted to reach.

    The Rails app under development, plus its loopback aliases (the agent
    prompts use ``localhost:3000`` interchangeably with the Docker hostname),
    plus anything the operator added via ``BROWSER_ALLOWED_ORIGINS``.
    """
    origins: set[str] = set()

    rails_base = os.getenv("RAILS_BASE_URL", DEFAULT_RAILS_BASE_URL)
    rails_origin = _origin(rails_base)
    if rails_origin:
        origins.add(rails_origin)
        rails_port = rails_origin.rsplit(":", 1)[1]
        origins.add(f"localhost:{rails_port}")
        origins.add(f"127.0.0.1:{rails_port}")

    for extra in os.getenv(ALLOWED_ORIGINS_ENV, "").split(","):
        extra = extra.strip()
        if not extra:
            continue
        # Accept both "http://host:port" and a bare "host:port".
        extra_origin = _origin(extra if "//" in extra else f"http://{extra}")
        if extra_origin:
            origins.add(extra_origin)

    return frozenset(origins)


def _reject_if_internal(ip_text: str, url: str) -> None:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        raise UrlNotAllowed(f"Blocked: could not interpret address {ip_text!r} for {url}")

    # ::ffff:127.0.0.1 is loopback wearing an IPv6 costume.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise UrlNotAllowed(
            f"Blocked: {url} resolves to the internal address {ip} — this tool may only "
            f"reach the app's own origin or public internet addresses."
        )


def validate_outbound_url(url: str, *, allow_internal: Optional[Iterable[str]] = None) -> str:
    """Raise :class:`UrlNotAllowed` unless the server may fetch ``url``.

    Returns the URL unchanged so it can be used inline at the call site.
    """
    if not url or not isinstance(url, str):
        raise UrlNotAllowed("Blocked: no URL provided.")

    parts = urlsplit(url.strip())

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlNotAllowed(
            f"Blocked: scheme {parts.scheme or '(none)'!r} is not permitted — "
            f"only {' and '.join(ALLOWED_SCHEMES)} URLs can be opened."
        )

    host = parts.hostname
    if not host:
        raise UrlNotAllowed(f"Blocked: {url} has no hostname.")

    origin = _origin(url)
    permitted = set(allowed_internal_origins())
    if allow_internal:
        permitted.update(allow_internal)
    if origin in permitted:
        return url

    # A literal IP needs no resolution — check it directly.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _reject_if_internal(host, url)
        return url

    try:
        resolved = socket.getaddrinfo(host, parts.port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError) as e:
        raise UrlNotAllowed(f"Blocked: could not resolve {host!r} ({e}).")

    if not resolved:
        raise UrlNotAllowed(f"Blocked: {host!r} resolved to no addresses.")

    # Every A/AAAA record must be public — one public record does not launder
    # a private one sitting beside it.
    for family, _type, _proto, _canon, sockaddr in resolved:
        _reject_if_internal(sockaddr[0], url)

    return url


def guarded_route_handler(allow_internal: Optional[Iterable[str]] = None):
    """Build a Playwright ``page.route`` handler that re-checks every request.

    The pre-flight check on the initial URL does not cover where a redirect
    lands, or what the page then asks the browser to fetch. This aborts any
    request whose destination fails the same validation, so a page cannot use
    the headless browser as a proxy into the internal network.
    """
    verdict_cache: dict[str, bool] = {}

    def _handler(route, request=None):
        target = getattr(route, "request", None)
        url = getattr(target, "url", None) or getattr(request, "url", "")
        origin = _origin(url) or url

        allowed = verdict_cache.get(origin)
        if allowed is None:
            try:
                validate_outbound_url(url, allow_internal=allow_internal)
                allowed = True
            except UrlNotAllowed:
                allowed = False
            verdict_cache[origin] = allowed

        if allowed:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    return _handler
