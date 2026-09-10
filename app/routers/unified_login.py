"""Unified Login Phase 2 — the box side of llamapress.ai-as-identity-provider.

``GET /auth/consume`` is the new front door: the mothership mints a short-lived
opaque grant and redirects the browser here with ``?token=``. We redeem it
server-to-server (``MothershipClient.verify_login_grant`` — no shared secret),
sign the user in locally as a shadow user keyed by a stable
``llamapress_user_guid``, and hand off to the chat, threading the SAME raw token
to the Rails iframe as ``rails_token`` (Phase 3 redeems it once more with
``audience=rails_app``).

Everything here is INERT until the mothership flips ``unified_login_mode`` on —
nothing links to ``/auth/consume`` until then. The legacy ``GET /login?token=``
HMAC magic-link path (app/routers/ui.py) is the permanent fallback and is left
completely untouched.

Design contract (frozen, mirrored by prod + the Phase 3 gem):
  * never key a user by email — only by guid, with link_username as the one-time
    adoption key for the admin account created at claim time;
  * grant_expired / grant_used are EXPECTED (refresh, bookmark) and degrade
    gracefully — never an error-page dead end;
  * one retry bounce max (loop breaker via the ``retry`` param);
  * every user-facing link back to the mothership uses the brand domain the
    browser arrived from (``?sso_origin=``, remembered in a cookie — see
    app/services/sso_origin.py), never the server-to-server ``mothership_url``.
"""

import logging
import secrets
from html import escape
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import engine
from app.dependencies import _user_from_session_cookie
from app.models import User
from app.routers.ui import _set_session_cookie
from app.services.mothership_client import MothershipClient
from app.services.sso_origin import (
    brand_display_name,
    remember_sso_origin,
    resolve_sso_origin,
)
from app.services.user_service import get_user_by_username, hash_password
from app.lib.cors_preflight import preflight_response

logger = logging.getLogger(__name__)

router = APIRouter()

# Hand-off params the chat JS reads on load (same set the /login allowlist
# forwards). Threaded through the post-auth redirect so an auto-fired build runs
# on the intended model/mode.
PASSTHROUGH_KEYS = ("prompt", "llm_model", "agent_mode")

# Grant-level failures where re-authorizing at the mothership (which mints a
# FRESH token) can recover — the only codes we bounce for.
BOUNCE_CODES = frozenset({"grant_expired", "grant_used", "grant_not_found"})

# HTTP status to return from the Phase 3 redeem proxy for each known error code.
# The gem's redeem_rails_grant reads ``error_code`` off the body regardless of
# status, so these are cosmetic-but-honest; anything unmapped is a 502 (a bounce
# can't help the gem, mirroring how a bad gateway reads to its caller).
REDEEM_ERROR_STATUS = {
    "grant_not_found": 404,
    "grant_expired": 410,
    "grant_used": 409,
    "bad_audience": 422,
    "mothership_unreachable": 502,
}


def _passthrough_params(request: Request) -> dict:
    """Collect the non-empty hand-off params from the incoming request."""
    out = {}
    for key in PASSTHROUGH_KEYS:
        val = request.query_params.get(key)
        if val:
            out[key] = val
    return out


def _sanitize_return_to(return_to: str) -> str | None:
    """Reduce ``return_to`` to a safe same-origin PATH, or None.

    Rejects absolute/protocol-relative URLs (open-redirect guard) — only an
    in-app path beginning with a single ``/`` is honored.
    """
    if not return_to:
        return None
    parsed = urlparse(return_to)
    if parsed.scheme or parsed.netloc:
        return None
    path = parsed.path or ""
    if not path.startswith("/") or path.startswith("//"):
        return None
    # Preserve any query/fragment the caller attached to the path.
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def _sso_url(mothership: MothershipClient, request: Request, *, retry: bool) -> str | None:
    """Build the mothership recovery URL (``/sso/leo/{instance_name}``).

    Returns None if the mothership isn't configured (nowhere to bounce to).
    ``retry=True`` adds ``retry=1`` so the mothership knows to bounce back here
    with a fresh token AND so our loop breaker fires if that fresh token also
    fails. The "Continue with LlamaPress" link uses ``retry=False``.

    The host comes from ``resolve_sso_origin`` — the brand domain this browser
    actually arrived from (builtwithleo.com vs llamapress.ai), NOT the
    server-to-server ``mothership_url``. Bouncing a builtwithleo.com user to
    llamapress.ai lands them on a different Rails session and a sign-in wall.
    """
    name = mothership.instance_name
    base = resolve_sso_origin(request, mothership.mothership_url or "")
    if not base or not name:
        return None
    params = _passthrough_params(request)
    # Same host-only-cookie reason as the login CTA (see ui._render_sso_login_cta):
    # without this the recovery bounce drops the host and a user who recovers
    # through it still lands on the canonical host. Set HERE rather than in
    # _passthrough_params, because that helper also builds the redirect INTO the
    # chat (with rails_token) and return_host has no business on that hop.
    host = (request.headers.get("host") or "").strip() if request else ""
    if host:
        params["return_host"] = host
    if retry:
        params["retry"] = "1"
    query = urlencode(params)
    url = f"{base.rstrip('/')}/sso/leo/{name}"
    return f"{url}?{query}" if query else url


async def resolve_shadow_user(
    session: Session, payload: dict, mothership: MothershipClient
) -> User:
    """Resolve (or provision) the local shadow user for a verified grant.

    Order matters, and matching is NEVER done by email:
      1. Match by ``llamapress_user_guid`` — sync email/display_name if drifted.
         Never renames an existing account.
      2. Else adopt ``link_username`` ONE TIME: if that account exists with a
         NULL guid, stamp the guid onto it (preserves thread history and
         visible_agents from the claim-time admin user). If it's already linked
         to a DIFFERENT guid, that's a data-integrity smell — report and fall
         through to (3) rather than hijack it.
      3. Else create a fresh user with an unusable password, *named* after the
         mothership-verified email (falling back to ``lp-<guid[:12]>``).

    Note the distinction in (3): the email is used to NAME a new account, never to
    FIND an existing one. Naming is safe because ``payload`` only exists after
    ``grant_redeemer`` verified it server-to-server; matching on it would be the
    spoofable email-login this function deliberately avoids.
    """
    user_obj = payload.get("user") or {}
    guid = user_obj.get("guid")
    email = user_obj.get("email")
    display_name = user_obj.get("name")
    role = payload.get("role")
    link_username = payload.get("link_username")

    # 1. Match by guid.
    if guid:
        existing = session.exec(
            select(User).where(User.llamapress_user_guid == guid)
        ).first()
        if existing:
            changed = False
            if email and existing.email != email:
                existing.email = email
                changed = True
            if display_name and existing.display_name != display_name:
                existing.display_name = display_name
                changed = True
            if changed:
                session.add(existing)
                session.commit()
                session.refresh(existing)
            return existing

    # 2. One-time adoption of the claim-time admin account via link_username.
    if link_username:
        candidate = get_user_by_username(session, link_username)
        if candidate is not None:
            if candidate.llamapress_user_guid is None:
                candidate.llamapress_user_guid = guid
                if email:
                    candidate.email = email
                if display_name:
                    candidate.display_name = display_name
                session.add(candidate)
                session.commit()
                session.refresh(candidate)
                return candidate
            if candidate.llamapress_user_guid == guid:
                # Already linked to this same guid (step 1 normally catches this;
                # defensive for a race). Nothing to adopt.
                return candidate
            # Linked to a DIFFERENT guid — do not hijack. Report and create fresh.
            await mothership.report_error(
                thread_id=None,
                error_class="UnifiedLogin::ShadowUserError",
                error_message=(
                    f"link_username '{link_username}' already bound to a different guid"
                ),
                traceback_str="",
            )

    # 3. Create a fresh shadow user. Prefer the mothership-VERIFIED email as the
    #    username — it arrives server-to-server over the bearer channel after
    #    grant_redeemer succeeds, so it is not user input, and `alice@corp.com` beats
    #    `lp-a1b2c3d4e5f6` everywhere an operator has to recognize the account. Falls
    #    back to lp-<guid> when the grant carries no email. Unusable password (bcrypt
    #    of 32 random bytes) keeps password_hash NOT NULL while making local password
    #    login impossible for this user.
    username = email or f"lp-{(guid or secrets.token_hex(8))[:12]}"
    if get_user_by_username(session, username) is not None:
        username = f"{username}-{secrets.token_hex(2)}"
    new_user = User(
        username=username,
        password_hash=hash_password(secrets.token_hex(32)),
        role="engineer",
        is_admin=role in ("owner", "admin"),
        is_active=True,
        llamapress_user_guid=guid,
        email=email,
        display_name=display_name,
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)
    return new_user


def _login_page_with_error(mothership: MothershipClient, request: Request) -> HTMLResponse:
    """Render the sign-in page with an error banner + a manual recovery link.

    The terminal failure surface (loop broken, or nothing a bounce can fix).
    Served 200 — it's a page, not an API error.
    """
    try:
        with open("login.html") as f:
            html = f.read()
    except Exception:
        html = "<html><body><p>Sign-in failed. Please try again.</p></body></html>"

    banner = (
        '<div class="message error">Your sign-in link has expired or already '
        "been used. Please sign in again.</div>"
    )
    sso = _sso_url(mothership, request, retry=False)
    if sso:
        brand = escape(brand_display_name(sso))
        banner += (
            f'<div style="margin-top:16px;text-align:center;">'
            f'<a href="{escape(sso, quote=True)}" '
            f'style="color:#a78bfa;text-decoration:underline;">'
            f"Continue with {brand}</a></div>"
        )
    # login.html carries an empty <div id="message"></div> placeholder.
    if '<div id="message"></div>' in html:
        html = html.replace('<div id="message"></div>', f'<div id="message">{banner}</div>')
    else:
        html = html.replace("</body>", f'<div id="message">{banner}</div></body>')
    return HTMLResponse(content=html)


@router.options("/auth/consume")
async def auth_consume_preflight(request: Request):
    """Answer the CORS preflight instead of 400ing it (see app/lib/cors_preflight)."""
    return preflight_response(request)


@router.get("/auth/consume")
async def auth_consume(
    request: Request,
    token: str = "",
    return_to: str = "",
    retry: str = "",
):
    """Redeem a mothership login grant and sign the user in locally.

    See the module docstring for the full contract. This endpoint is never a
    wall: with no token it defers to /login, and every failure degrades to
    either a silent redirect, a single recovery bounce, or the login page.
    """
    # 1. No token → defer to the legacy front door (never a wall here).
    if not token:
        return RedirectResponse(url="/login", status_code=302)

    mothership = MothershipClient()
    payload, error_code = await mothership.verify_login_grant(token, "llamabot")

    # 3. Success → shadow user + session cookie + hand off to the chat.
    if payload is not None:
        with Session(engine) as session:
            user = await resolve_shadow_user(session, payload, mothership)

        params = _passthrough_params(request)
        # Thread the SAME raw token to the Rails iframe (Phase 3 redeems it with
        # audience=rails_app). The frontend strips it from the URL bar after use.
        params["rails_token"] = token
        dest = _sanitize_return_to(return_to) or "/"
        sep = "&" if "?" in dest else "?"
        redirect_url = f"{dest}{sep}{urlencode(params)}"

        response = RedirectResponse(url=redirect_url, status_code=302)
        _set_session_cookie(response, user)
        # Remember which brand domain this browser came from so a LATER bounce
        # (expired grant, sign-out) returns to it rather than to the configured
        # mothership_url. Not threaded into redirect_url — the chat has no use
        # for it and it would sit in the address bar.
        remember_sso_origin(request, response, mothership.mothership_url or "")
        return response

    # 4. Failure paths.
    # 4a. A stale token on a page the user already has a session for is NOT an
    #     error (the most common case: refresh/bookmark). Silently land them home.
    with Session(engine) as session:
        current = _user_from_session_cookie(request, session)
    if current is not None:
        return RedirectResponse(url="/", status_code=302)

    # 4b. Recoverable grant failure, first attempt → bounce to the mothership to
    #     re-authorize (it returns with a FRESH token + retry=1). One bounce max.
    if error_code in BOUNCE_CODES and not retry:
        sso = _sso_url(mothership, request, retry=True)
        if sso:
            response = RedirectResponse(url=sso, status_code=302)
            remember_sso_origin(request, response, mothership.mothership_url or "")
            return response

    # 4c/4d. Terminal: loop already broken (retry present), mothership_unreachable
    #     (a bounce can't help), or an unexpected code. Report the stuck user and
    #     render the login page with a manual recovery link.
    await mothership.report_error(
        thread_id=None,
        error_class="UnifiedLogin::ConsumeFailed",
        error_message=error_code or "unknown",
        traceback_str="",
    )
    response = _login_page_with_error(mothership, request)
    remember_sso_origin(request, response, mothership.mothership_url or "")
    return response


class RedeemRailsGrantRequest(BaseModel):
    token: str = ""


@router.post("/internal/redeem_rails_grant")
async def redeem_rails_grant(body: RedeemRailsGrantRequest):
    """Redeem a login grant on behalf of the Rails app (Phase 3, Option 1).

    The generated Rails app (``llama_bot_rails`` gem) lives in a container with
    NO mothership credentials, so it cannot redeem grants itself. Its
    ``LlamaBotRails::LlamaBot.redeem_rails_grant`` POSTs the raw grant here; we
    redeem it a SECOND time (grants are single-use *per audience*) with our own
    creds against ``audience=rails_app`` and relay the mothership's answer
    verbatim. This keeps the mothership token out of the Rails container — the
    entire point of Unified Login.

    Contract the gem pins (redeem_rails_grant_spec.rb):
      * success → HTTP 200 with the mothership payload unchanged (the gem reads
        ``user.guid`` / ``role`` / ``permissions`` straight off it);
      * failure → ``{"success": false, "error_code": ...}`` (non-200); the gem
        surfaces ``error_code`` and degrades gracefully — never a wall.

    Redemption requires holding a valid, unexpired, single-use grant, which is
    itself the credential — so this needs no extra auth beyond the trusted
    LlamaBot↔Rails channel, consistent with the gem's other internal calls.
    """
    token = (body.token or "").strip()
    if not token:
        return JSONResponse(
            {"success": False, "error_code": "missing_token"}, status_code=400
        )

    mothership = MothershipClient()
    payload, error_code = await mothership.verify_login_grant(token, "rails_app")

    if payload is not None:
        # Relay verbatim — payload already carries ``success: true`` + user/role.
        return JSONResponse(payload)

    status = REDEEM_ERROR_STATUS.get(error_code, 502)
    return JSONResponse(
        {"success": False, "error_code": error_code or "verify_failed"},
        status_code=status,
    )
