"""Which mothership domain a user came from — the SSO return-to-brand contract.

The mothership Rails app answers on more than one domain (``llamapress.ai`` and
``builtwithleo.com``), but a box only knows the one baked into
``.leonardo/instance.json`` as ``mothership_url``. So a user who signed in at
builtwithleo.com and later needs to re-authorize got bounced to llamapress.ai —
a different domain, therefore a different Rails session, therefore a surprise
"sign in" wall on a brand they never used.

The fix is a threaded param, not a guess: the mothership appends
``sso_origin=<its own base URL>`` when it redirects a browser into the box
(``/auth/consume``, and the legacy ``/login?token=`` magic link). We validate it
against an allowlist, remember it in a cookie, and build every later
user-facing mothership link from it. Nothing else about the flow changes —
server-to-server calls still go to the configured ``mothership_url``, which is
the credentialed channel.

Guessing was considered and rejected: instance boxes all live on
``*.llamapress.ai``, so the Host header carries no brand signal, and ``Referer``
is routinely stripped across a cross-origin redirect.

Open-redirect safety: only an allowlisted host is ever honored. Anything else
(including a plausible-looking lookalike) falls back to the configured
mothership URL, so a crafted ``?sso_origin=`` cannot aim the sign-in link at an
attacker's page.
"""

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Cookie remembering the brand domain for this browser. Server-read only.
SSO_ORIGIN_COOKIE = "leo_sso_origin"

#: A year — the origin is a branding preference, not a credential, and it should
#: outlive the session cookie so a returning user still sees "their" domain.
SSO_ORIGIN_MAX_AGE = 365 * 24 * 3600

#: Apex domains the mothership answers on. A host matches when it IS one of
#: these or is a subdomain of one. Extend without a release via the
#: ``SSO_ORIGIN_HOSTS`` env var (comma-separated apex domains).
DEFAULT_SSO_ORIGIN_HOSTS = ("llamapress.ai", "builtwithleo.com")

#: How each brand names itself in sign-in copy. Unlisted hosts use the bare host.
BRAND_DISPLAY_NAMES = {
    "llamapress.ai": "LlamaPress.ai",
    "builtwithleo.com": "Leo",
}


def _env_hosts() -> tuple:
    raw = os.getenv("SSO_ORIGIN_HOSTS", "")
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _host_of(url: str) -> str:
    """Lowercased hostname of a URL (or of a bare host string), no port."""
    if not url:
        return ""
    candidate = url if "//" in url else f"//{url}"
    return (urlparse(candidate).hostname or "").lower()


def allowed_hosts(mothership_url: str = "") -> tuple:
    """Apex domains an ``sso_origin`` may point at.

    The configured ``mothership_url`` is always allowed — a self-hosted or
    staging mothership is by definition the trusted one for that box.
    """
    hosts = list(DEFAULT_SSO_ORIGIN_HOSTS) + list(_env_hosts())
    configured = _host_of(mothership_url)
    if configured:
        hosts.append(configured)
    return tuple(dict.fromkeys(hosts))


def sanitize_sso_origin(value: str, mothership_url: str = "") -> str | None:
    """Normalize a claimed origin to ``scheme://host``, or None if not allowed.

    Accepts a full URL or a bare host. Path, query and fragment are dropped —
    we only ever want the base to hang ``/sso/leo/{name}`` off.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None

    parsed = urlparse(value if "//" in value else f"//{value}")
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None

    if not any(host == a or host.endswith(f".{a}") for a in allowed_hosts(mothership_url)):
        logger.info("Ignoring sso_origin for non-allowlisted host: %s", host)
        return None

    # Keep an explicit port (dev/staging mothership on :3000); default to https
    # so a scheme-less or http claim can't downgrade a public brand domain.
    scheme = parsed.scheme or "https"
    if scheme == "http" and host not in ("localhost", "127.0.0.1") and not parsed.port:
        scheme = "https"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return f"{scheme}://{netloc}"


def resolve_sso_origin(request, mothership_url: str = "") -> str | None:
    """The brand base URL to build user-facing mothership links from.

    Precedence: this request's ``?sso_origin=`` → the remembered cookie → the
    configured ``mothership_url``. Returns None when nothing is configured
    (self-hosted box with no mothership — the caller renders no SSO link).
    """
    claimed = ""
    try:
        claimed = request.query_params.get("sso_origin") or ""
    except Exception:
        claimed = ""
    origin = sanitize_sso_origin(claimed, mothership_url)
    if origin:
        return origin

    try:
        remembered = request.cookies.get(SSO_ORIGIN_COOKIE) or ""
    except Exception:
        remembered = ""
    origin = sanitize_sso_origin(remembered, mothership_url)
    if origin:
        return origin

    return (mothership_url or "").rstrip("/") or None


def remember_sso_origin(request, response, mothership_url: str = "") -> None:
    """Persist this request's ``?sso_origin=`` on the response, if it's valid.

    Only ever writes a validated, allowlisted origin — an unknown or malformed
    claim leaves whatever the browser already had alone.
    """
    try:
        claimed = request.query_params.get("sso_origin") or ""
    except Exception:
        return
    origin = sanitize_sso_origin(claimed, mothership_url)
    if not origin:
        return
    # Imported late: token_service reads env at import time and this module is
    # pulled in by tests that don't boot the app.
    from app.services.token_service import SESSION_COOKIE_SECURE

    response.set_cookie(
        key=SSO_ORIGIN_COOKIE,
        value=origin,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
        max_age=SSO_ORIGIN_MAX_AGE,
    )


def brand_display_name(origin: str) -> str:
    """How to name the brand behind ``origin`` in sign-in copy."""
    host = _host_of(origin)
    for apex, label in BRAND_DISPLAY_NAMES.items():
        if host == apex or host.endswith(f".{apex}"):
            return label
    return host or "LlamaPress.ai"
