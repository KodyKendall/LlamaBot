"""UI/HTML page routes for LlamaBot."""

import json
import logging
import os
from urllib.parse import urlencode
from html import escape
from pathlib import Path

from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session

from app.db import engine
from app.models import User
from app.dependencies import (
    security, get_db_session, auth, get_current_user, admin_required, has_any_users,
    engineer_or_admin_required, try_authenticate
)
from app.services.user_service import authenticate_user, get_user_by_username
from app.services.token_service import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_DAYS,
    create_session_token,
)
from app.services.magic_link_service import (
    MagicLinkInvalid,
    MagicLinkSecretMissing,
    verify_magic_link_token,
)
from app.services.mothership_client import MothershipClient
from app.services.sso_origin import (
    brand_display_name,
    remember_sso_origin,
    resolve_sso_origin,
)

# Role → agent-mode permissions live in app/permissions.py — the single source of
# truth this router and the WebSocket gate both read. Re-exported here because
# load_custom_agent_modes() validates against the built-in keys.
from app.permissions import BUILTIN_AGENT_MODE_KEYS, visible_modes  # noqa: F401
from app.lib.cors_preflight import preflight_response

logger = logging.getLogger(__name__)


# ============== Browser-pane tabs ==============
#
# Which tabs the chat UI's browser pane shows, as a site setting. The App tab is
# deliberately NOT in this catalog: it is the only surface that shows the user
# their own application, so it is always visible and is never offered as a
# choice — a box whose tab strip could be emptied has no way back to itself.

ALWAYS_VISIBLE_TAB = "liveSiteFrame"
VISIBLE_TABS_SETTING_KEY = "visible_tabs"

#: The Code tab is deliberately NOT in OPTIONAL_BROWSER_TABS. It is owned by the
#: `enable_vscode` setting instead, because the tab and the code-server
#: container have to move together — see app/services/vscode_service.py.
VSCODE_TAB = "vsCodeFrame"

#: Tickets, Feedback and Messages used to be three separate tabs here. They are
#: one "Inbox" tab now, because they are one place: the Rails app renders its own
#: tab bar across all five inbox surfaces (Tickets, Feedback, Requests, Messages,
#: Notifications), so switching between them no longer needs a browser tab each.
#: Requests and Notifications never had a tab at all and were reachable only by
#: links inside other pages; folding them into that bar is what made them
#: navigable. Activity stays separate — it is an audit log, not a queue.
OPTIONAL_BROWSER_TABS = [
    {"target": "inboxFrame", "label": "Inbox", "icon": "fa-inbox",
     "description": "Tickets, feedback, requests, messages and notifications, with an unread count on the tab"},
    {"target": "activityFrame", "label": "Activity", "icon": "fa-clock-rotate-left",
     "description": "Audit log, record history, and adoption metrics"},
]


def resolve_visible_tabs(session) -> list[str]:
    """The tab targets the browser pane should show, App always included.

    The setting is stored as a comma-separated list of targets. Never having
    touched it means "show everything", so an existing box keeps its current tab
    strip after this ships rather than silently losing tabs. An empty stored
    value is a real choice (App only) and is honored.

    Unknown names in a stored value are dropped, so a tab that is renamed or
    removed later can't leave a dead entry switched on forever. "vsCodeFrame" is
    one of those unknown names now: an old stored value that still lists it
    cannot switch the Code tab back on, because only `enable_vscode` can.
    """
    from app.routers.api import get_site_setting
    from app.services.vscode_service import vscode_enabled

    known = [t["target"] for t in OPTIONAL_BROWSER_TABS]
    raw = get_site_setting(session, VISIBLE_TABS_SETTING_KEY, default=None)

    if raw is None:
        chosen = known
    else:
        wanted = {name.strip() for name in raw.split(",") if name.strip()}
        chosen = [target for target in known if target in wanted]

    # The Code tab appears only when the editor itself is switched on.
    if vscode_enabled(session):
        chosen = [VSCODE_TAB] + chosen

    return [ALWAYS_VISIBLE_TAB] + chosen


def load_custom_agent_modes(overlay_path, graphs):
    """Load + validate per-instance custom agent modes from an overlay file.

    Custom modes let an instance surface its own LangGraph agent in the chat mode
    dropdown without rebuilding the image. The overlay file (``agent_modes.json``,
    mounted next to the editable ``langgraph.json``) is a JSON array of objects::

        [{ "key", "label", "agent_name", "description"?, "shortLabel"?, "icon"? }]

    Validation (this is the brittle part — keep it strict so a broken entry can
    never reach the dropdown):
      * file missing / unreadable / invalid JSON / not a list -> [] (stock case)
      * an entry needs non-empty string ``key``, ``label``, ``agent_name``
      * ``agent_name`` must be a registered graph in ``langgraph.json`` (else it
        could never route — drop it rather than show a dead option)
      * ``key`` must not shadow a built-in mode (back-compat: built-ins win)
      * duplicate custom keys: first wins

    Returns a sanitized list of dicts safe to JSON-inject into the frontend.
    """
    try:
        with open(overlay_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, OSError):
        return []
    except json.JSONDecodeError as e:
        logger.warning(f"Ignoring custom agent modes overlay {overlay_path}: invalid JSON ({e})")
        return []

    if not isinstance(raw, list):
        logger.warning(f"Ignoring custom agent modes overlay {overlay_path}: expected a JSON array")
        return []

    registered = set(graphs or {})
    out = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        label = entry.get("label")
        agent_name = entry.get("agent_name")
        if not (isinstance(key, str) and key.strip()
                and isinstance(label, str) and label.strip()
                and isinstance(agent_name, str) and agent_name.strip()):
            logger.warning(f"Dropping custom agent mode (missing key/label/agent_name): {entry!r}")
            continue
        if key in BUILTIN_AGENT_MODE_KEYS:
            logger.warning(f"Dropping custom agent mode '{key}': cannot shadow a built-in mode")
            continue
        if agent_name not in registered:
            logger.warning(
                f"Dropping custom agent mode '{key}': agent_name '{agent_name}' "
                f"is not registered in langgraph.json graphs"
            )
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "key": key,
            "label": label,
            "agent_name": agent_name,
            "description": entry.get("description") if isinstance(entry.get("description"), str) else "",
            "shortLabel": entry.get("shortLabel") if isinstance(entry.get("shortLabel"), str) and entry.get("shortLabel").strip() else label,
            "icon": entry.get("icon") if isinstance(entry.get("icon"), str) else None,
        })
    return out


def _read_leonardo_value(filename: str) -> str | None:
    path = f".leonardo/{filename}"
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
        return value or None
    except Exception as e:
        logger.warning(f"Could not read {path}: {e}")
        return None

router = APIRouter()

# Frontend directory path
frontend_dir = Path(__file__).parent.parent / "frontend"

# Placeholder in login.html where the Unified Login CTA is injected server-side.
_SSO_CTA_PLACEHOLDER = "<!--SSO_CTA-->"


def _render_sso_login_cta(request: Request = None) -> str:
    """Build the "Sign in with your <brand> account" CTA for the login page.

    Unified Login's discoverable entry point: a link that top-navigates to the
    mothership's ``/sso/leo/{instance_name}`` SSO endpoint (which mints a grant
    and bounces back to ``/auth/consume``). ``target="_top"`` is REQUIRED — the
    login page can render inside the chat's app-preview iframe and the
    mothership won't load framed, so the click must break out to the top window.

    Host and copy both follow the brand domain this browser arrived from
    (``resolve_sso_origin``): a builtwithleo.com user gets sent back to
    builtwithleo.com, not to the ``mothership_url`` in instance.json.

    Returns "" on self-hosted instances (no mothership configured) so those users
    see the unchanged stock username/password form — never a broken button.
    """
    mothership = MothershipClient()
    name = mothership.instance_name
    base = resolve_sso_origin(request, mothership.mothership_url or "") if request \
        else (mothership.mothership_url or "").rstrip("/") or None
    if not base or not name:
        return ""
    sso_url = f"{base.rstrip('/')}/sso/leo/{name}"
    # The browser's llamabot_session cookie is HOST-ONLY, so tell the mothership
    # which host to land us back on. Without this, a box reached through a custom
    # domain gets its cookie set on the CANONICAL host and the login page returns
    # forever — crm.llamapress.ai on box crm-4, 22 /login hits and 6 complete SSO
    # round trips in 30 hours, every grant verified 200 OK (2026-09-08).
    #
    # A separate axis from sso_origin above: that one picks the BRAND
    # (llamapress.ai vs builtwithleo.com), this one picks which host OF THIS BOX to
    # come back to. A crm.builtwithleo.com user needs both.
    #
    # Not an open redirect: the mothership validates this against the hosts the box
    # verifiably serves (canonical, its builtwithleo mirror, or a verified custom
    # domain on the chat port) before attaching a grant, and ignores anything else.
    host = (request.headers.get("host") or "").strip() if request else ""
    if host:
        sso_url = f"{sso_url}?{urlencode({'return_host': host})}"
    # brand_display_name reads the URL's host, so the query string is irrelevant to it.
    brand = escape(brand_display_name(sso_url))
    return (
        f'<a class="sso-cta" href="{escape(sso_url, quote=True)}" target="_top">'
        f"Sign in with your {brand} account</a>"
        '<div class="sso-divider"><span>or</span></div>'
    )


@router.get("/")
async def root(request: Request):
    # If no users exist, redirect to registration
    if not has_any_users():
        return RedirectResponse(url="/register", status_code=302)

    # Otherwise require authentication (session cookie OR Basic Auth).
    # Unauthenticated browser users are redirected to /login so they get the
    # nice HTML form rather than the browser's native Basic Auth dialog.
    # Basic Auth still works for curl / scripted callers — they pass an
    # Authorization header and never see the redirect.
    with Session(engine) as session:
        user = try_authenticate(request, session)
        if not user:
            return RedirectResponse(url="/login", status_code=302)

        # Load per-instance custom agent modes (optional overlay, no image change).
        # Validated against the registered graphs (platform base ∪ client overlay).
        graphs = {}
        try:
            from app.lib.langgraph_registry import load_graphs
            graphs = load_graphs()
        except Exception as e:
            logger.warning(f"Could not read langgraph registry for custom agent modes: {e}")
        try:
            custom_agent_modes = load_custom_agent_modes("agent_modes.json", graphs)
        except Exception as e:
            logger.warning(f"Could not load custom agent modes: {e}")
            custom_agent_modes = []

        # The dropdown is a VIEW of the role's grant — same resolver the WebSocket
        # gates on (app/permissions.py). Hiding a mode here is a convenience, not
        # the control: web_socket_handler rejects an ungranted agent_name outright.
        visible_agents = visible_modes(
            session,
            user,
            custom={m["key"]: m["agent_name"] for m in custom_agent_modes},
        )

        # Serve the chat.html file with user role and visible agents injected
        with open(frontend_dir / "chat.html") as f:
            html = f.read()
        # Read site settings
        from app.routers.api import get_site_setting
        show_token_wheel = get_site_setting(session, "show_token_wheel", "false") == "true"
        proactive_build = get_site_setting(session, "proactive_build_after_ticket", "false") == "true"
        visible_tabs = resolve_visible_tabs(session)

        # Sleep lock, injected so a locked instance paints the modal on the FIRST
        # frame — the /api/instance-lock poll is what catches a lock that lands
        # while the tab is already open.
        from app.services.instance_lock import get_lock_state
        instance_lock = get_lock_state(session)

        # Inject user role, visible agents, and PostHog config as global variables for the frontend
        posthog_key = os.getenv("LLAMABOT_POSTHOG_KEY", "")
        posthog_host = os.getenv("LLAMABOT_POSTHOG_HOST", "")
        enable_github_button = os.getenv("ENABLE_GITHUB_BUTTON", "").lower() == "true"
        llamapress_user_id = _read_leonardo_value("LLAMAPRESS_USER_ID.txt")
        llamapress_email = _read_leonardo_value("LLAMAPRESS_EMAIL.txt")
        config_script = f'''<script>
window.LLAMABOT_USER_ROLE = "{getattr(user, "role", "engineer")}";
window.LLAMABOT_IS_ADMIN = {"true" if getattr(user, "is_admin", False) else "false"};
window.LLAMABOT_VISIBLE_AGENTS = {json.dumps(visible_agents)};
window.LLAMABOT_VISIBLE_TABS = {json.dumps(visible_tabs)};
window.LLAMABOT_SHOW_TOKEN_WHEEL = {"true" if show_token_wheel else "false"};
window.LLAMABOT_POSTHOG_KEY = {json.dumps(posthog_key) if posthog_key else "null"};
window.LLAMABOT_POSTHOG_HOST = {json.dumps(posthog_host) if posthog_host else "null"};
window.LLAMAPRESS_USER_ID = {json.dumps(llamapress_user_id) if llamapress_user_id else "null"};
window.LLAMAPRESS_EMAIL = {json.dumps(llamapress_email) if llamapress_email else "null"};
window.ENABLE_GITHUB_BUTTON = {"true" if enable_github_button else "false"};
window.LLAMABOT_PROACTIVE_BUILD = {"true" if proactive_build else "false"};
window.LLAMABOT_CUSTOM_AGENT_MODES = {json.dumps(custom_agent_modes)};
window.LLAMABOT_INSTANCE_LOCK = {json.dumps(instance_lock)};
</script>'''
        html = html.replace('</head>', f'{config_script}</head>')
        return HTMLResponse(content=html)


@router.get("/hello", response_class=JSONResponse)
async def hello():
    return {"message": "Hello, World! 🦙💬"}


@router.get("/register", response_class=HTMLResponse)
async def register_form():
    """Serve the registration form - only available when no users exist."""
    # Security: Only allow registration if no users exist
    if has_any_users():
        raise HTTPException(
            status_code=403,
            detail="Registration is disabled. Please contact an administrator to create an account."
        )

    with open("register.html") as f:
        html = f.read()
        return HTMLResponse(content=html)


@router.post("/register")
async def register(
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    session: Session = Depends(get_db_session)
):
    """Process registration form - only works when no users exist (creates first admin user)."""
    # Security: Only allow registration if no users exist
    if has_any_users():
        raise HTTPException(
            status_code=403,
            detail="Registration is disabled. Please contact an administrator to create an account."
        )

    # Validate passwords match
    if password != confirm:
        raise HTTPException(
            status_code=400,
            detail="Passwords do not match"
        )

    # Check if username already exists (shouldn't happen if no users, but be safe)
    existing_user = get_user_by_username(session, username)
    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Username already exists"
        )

    try:
        # First user is always an admin
        from app.services.user_service import create_admin_user
        user = create_admin_user(session, username, password)
        logger.info(f"Admin user '{username}' registered successfully (first user)")
        return JSONResponse({
            "message": f"Admin user '{username}' registered successfully! You can now use these credentials to access the app."
        })
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to create user"
        )


_SESSION_COOKIE_KWARGS = dict(
    key=SESSION_COOKIE_NAME,
    httponly=True,
    secure=SESSION_COOKIE_SECURE,
    samesite="lax",
    path="/",
    max_age=SESSION_TTL_DAYS * 24 * 3600,
)


def _set_session_cookie(response, user: User) -> None:
    response.set_cookie(value=create_session_token(user), **_SESSION_COOKIE_KWARGS)


@router.post("/login")
async def login(
    request: Request,
    session: Session = Depends(get_db_session),
):
    """Exchange username/password for a session cookie.

    Accepts either JSON `{"username": ..., "password": ...}` or
    `application/x-www-form-urlencoded` so password managers and basic <form>
    submissions both work.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        username = body.get("username")
        password = body.get("password")
    else:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")

    if not isinstance(username, str) or not isinstance(password, str):
        raise HTTPException(status_code=400, detail="username and password required")

    user = authenticate_user(session, username, password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    response = JSONResponse({"ok": True, "username": user.username})
    _set_session_cookie(response, user)
    return response


@router.options("/login")
async def login_preflight(request: Request):
    """Answer the CORS preflight instead of 400ing it (see app/lib/cors_preflight)."""
    return preflight_response(request)


@router.get("/login")
async def login_get(
    request: Request,
    token: str = "",
    session: Session = Depends(get_db_session),
):
    """Login endpoint — dual purpose.

    - `GET /login` (no token): serves the HTML sign-in form. Replaces the
      browser's native Basic Auth dialog as the front door for browser users.
    - `GET /login?token=<signed>`: magic-link sign-in. Used by the
      LlamaPress.ai Rails mothership to redirect a user into a freshly-claimed
      Leonardo instance with the user already signed in. Validates an
      HMAC-signed token, looks up the user by username, sets the session
      cookie, and redirects to /.

    Unknown username → 401 (no auto-provision; /register remains the only
    user-creation path). The Rails side guarantees the user exists before
    generating the magic-link token.
    """
    if not token:
        # No token → serve the sign-in form. If no users exist yet, push them
        # to registration instead.
        if not has_any_users():
            return RedirectResponse(url="/register", status_code=302)
        with open("login.html") as f:
            html = f.read()
        # Inject the Unified Login CTA on mothership-managed instances; on
        # self-hosted boxes the placeholder is replaced with "" (stock form).
        html = html.replace(_SSO_CTA_PLACEHOLDER, _render_sso_login_cta(request))
        response = HTMLResponse(content=html)
        # A mothership link into /login can carry ?sso_origin= too; remember it
        # so the CTA and any later bounce stay on that brand.
        remember_sso_origin(request, response, MothershipClient().mothership_url or "")
        return response

    try:
        username = verify_magic_link_token(token)
    except MagicLinkSecretMissing:
        # Operator-facing: the instance launcher forgot to wire the secret through.
        raise HTTPException(
            status_code=503,
            detail=(
                "Magic-link sign-in is not configured on this instance "
                "(LLAMAPRESS_AI_LOGIN_SECRET is unset). POST /login still works."
            ),
        )
    except MagicLinkInvalid as e:
        logger.info(f"Magic-link rejected: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = get_user_by_username(session, username)
    if not user or not user.is_active:
        # Intentional: 401, not auto-provision. Rails owns the sequencing fix.
        raise HTTPException(status_code=401, detail="Unknown user")

    # Preserve ?prompt= and ?conversation= params through the redirect
    from urllib.parse import urlencode
    forward_params = {}
    for key in ("prompt", "conversation", "welcome_prompt", "llm_model", "agent_mode"):
        val = request.query_params.get(key)
        if val:
            forward_params[key] = val
    redirect_url = f"/?{urlencode(forward_params)}" if forward_params else "/"

    response = RedirectResponse(url=redirect_url, status_code=302)
    _set_session_cookie(response, user)
    # The legacy magic link can carry ?sso_origin= as well, so a user who came in
    # from builtwithleo.com keeps that brand on any later sign-in prompt.
    remember_sso_origin(request, response, MothershipClient().mothership_url or "")
    return response


@router.get("/users", response_class=HTMLResponse)
async def users_page(admin: User = Depends(admin_required)):
    """Serve the admin user management page."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot User Management</title>
    <link rel="icon" type="image/png" href="/frontend/leonardo-icon.png">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {
            --bg-color: #0d0d1a;
            --chat-bg: #1a1730;
            --text-color: #e0e0e0;
            --border-color: rgba(139, 92, 246, 0.2);
            --accent-color: #8b5cf6;
        }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }
        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        .header {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 30px;
        }
        .back-btn {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            text-decoration: none;
            transition: background 0.2s;
        }
        .back-btn:hover { background: rgba(139, 92, 246, 0.15); }
        h1 { font-size: 1.5rem; margin: 0; }
        .card {
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }
        .card-header {
            font-size: 0.85rem;
            color: rgba(255, 255, 255, 0.5);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
        }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid var(--border-color); }
        th { color: rgba(255, 255, 255, 0.5); font-weight: 500; font-size: 0.85rem; }
        tr:last-child td { border-bottom: none; }
        .badge {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 500;
            text-transform: uppercase;
        }
        .badge-engineer { background: rgba(139, 92, 246, 0.2); color: #a78bfa; }
        .badge-user { background: rgba(167, 139, 250, 0.15); color: #c4b5fd; }
        .badge-admin { background: rgba(139, 92, 246, 0.2); color: #a78bfa; }
        .badge-active { background: rgba(34, 197, 94, 0.15); color: #22c55e; }
        .badge-inactive { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
        .btn {
            padding: 6px 12px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            margin-right: 4px;
            background: transparent;
            color: var(--text-color);
            transition: all 0.2s;
        }
        .btn:hover { background: rgba(139, 92, 246, 0.15); }
        .btn-danger { border-color: rgba(239, 68, 68, 0.25); color: #ef4444; }
        .btn-danger:hover { background: rgba(239, 68, 68, 0.15); }
        .form-row { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; }
        .form-group { flex: 1; min-width: 120px; }
        .form-group label { display: block; margin-bottom: 6px; font-size: 0.85rem; color: rgba(255, 255, 255, 0.5); }
        .form-group input, .form-group select {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            box-sizing: border-box;
        }
        .form-group input:focus, .form-group select:focus {
            outline: none;
            border-color: rgba(139, 92, 246, 0.4);
            box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.08);
        }
        .btn-primary {
            background: linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%);
            border-color: #8b5cf6;
            color: white;
            padding: 10px 20px;
        }
        .btn-primary:hover { opacity: 0.9; }
        .message {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: none;
        }
        .message.success { background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.25); color: #22c55e; display: block; }
        .message.error { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.25); color: #ef4444; display: block; }
        .actions { white-space: nowrap; }
        select.role-select {
            padding: 4px 8px;
            font-size: 12px;
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            border: 1px solid var(--border-color);
        }
        .card-hint {
            margin: 0 0 16px;
            font-size: 0.85rem;
            color: rgba(255, 255, 255, 0.5);
            line-height: 1.5;
        }
        .role-row {
            display: flex;
            gap: 16px;
            align-items: baseline;
            padding: 12px 0;
            border-top: 1px solid var(--border-color);
            flex-wrap: wrap;
        }
        .role-row:first-child { border-top: none; }
        .role-name {
            min-width: 90px;
            font-weight: 600;
            text-transform: capitalize;
        }
        .mode-grid {
            display: flex;
            gap: 8px 18px;
            flex-wrap: wrap;
            flex: 1;
        }
        .mode-check {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 0.85rem;
            cursor: pointer;
            white-space: nowrap;
        }
        .mode-check input { cursor: pointer; }
        #saveRoleModes { margin-top: 16px; }
        #saveRoleModes:disabled { opacity: 0.5; cursor: default; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/settings" class="back-btn">
                <i class="fa-solid fa-arrow-left"></i>
            </a>
            <h1>User Management</h1>
        </div>

        <div id="message" class="message"></div>

        <div class="card">
            <div class="card-header">Add New User</div>
            <form id="addUserForm" class="form-row">
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" id="newUsername" required>
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" id="newPassword" required>
                </div>
                <div class="form-group">
                    <label>Role</label>
                    <select id="newRole">
                        <option value="engineer">Engineer</option>
                        <option value="user">User</option>
                    </select>
                </div>
                <button type="submit" class="btn btn-primary">Add User</button>
            </form>
        </div>

        <div class="card">
            <div class="card-header">Users</div>
            <table>
                <thead>
                    <tr>
                        <th>Username</th>
                        <th>Role</th>
                        <th>Status</th>
                        <th>Created</th>
                        <th class="actions">Actions</th>
                    </tr>
                </thead>
                <tbody id="usersTable">
                    <tr><td colspan="5">Loading...</td></tr>
                </tbody>
            </table>
        </div>

        <div class="card">
            <div class="card-header">Role Permissions</div>
            <p class="card-hint">
                Which agent modes each role may use. Enforced on the server — a mode
                that's off here can't be reached, not just hidden. Admins always have
                every mode.
            </p>
            <div id="roleModes">Loading...</div>
            <button class="btn btn-primary" id="saveRoleModes" onclick="saveRoleModes()">Save Permissions</button>
        </div>
    </div>

    <script>
        let ALL_MODES = [];

        async function loadRoleModes() {
            const container = document.getElementById('roleModes');
            try {
                const response = await fetch('/api/role-modes');
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();
                ALL_MODES = data.all_modes;

                container.innerHTML = Object.keys(data.role_modes).sort().map(role => `
                    <div class="role-row" data-role="${role}">
                        <div class="role-name">${role}</div>
                        <div class="mode-grid">
                            ${ALL_MODES.map(mode => `
                                <label class="mode-check">
                                    <input type="checkbox" value="${mode}"
                                        ${data.role_modes[role].includes(mode) ? 'checked' : ''}>
                                    <span>${mode}${data.custom_modes.includes(mode) ? ' *' : ''}</span>
                                </label>
                            `).join('')}
                        </div>
                    </div>
                `).join('');

                if (data.custom_modes.length) {
                    container.innerHTML += '<p class="card-hint">* custom mode from this instance (agent_modes.json)</p>';
                }
            } catch (error) {
                container.textContent = 'Error loading role permissions: ' + error.message;
            }
        }

        async function saveRoleModes() {
            const btn = document.getElementById('saveRoleModes');
            btn.disabled = true;
            const role_modes = {};
            document.querySelectorAll('#roleModes .role-row').forEach(row => {
                // Read back in ALL_MODES order — that order drives the chat dropdown.
                role_modes[row.dataset.role] = Array.from(
                    row.querySelectorAll('input[type=checkbox]:checked')
                ).map(cb => cb.value);
            });

            try {
                const response = await fetch('/api/role-modes', {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role_modes})
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
                showMessage('Role permissions saved. Takes effect on the next message.', 'success');
            } catch (error) {
                showMessage('Error saving: ' + error.message, 'error');
                loadRoleModes();  // resync the checkboxes with what's actually stored
            } finally {
                btn.disabled = false;
            }
        }

        async function loadUsers() {
            try {
                const response = await fetch('/api/users');
                const users = await response.json();
                const tbody = document.getElementById('usersTable');

                if (users.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5">No users found</td></tr>';
                    return;
                }

                tbody.innerHTML = users.map(user => `
                    <tr>
                        <td><strong>${user.username}</strong>${user.is_admin ? ' <span class="badge badge-admin">Admin</span>' : ''}</td>
                        <td>
                            <select class="role-select" onchange="updateRole(${user.id}, this.value)">
                                <option value="engineer" ${user.role === 'engineer' ? 'selected' : ''}>Engineer</option>
                                <option value="user" ${user.role === 'user' ? 'selected' : ''}>User</option>
                            </select>
                        </td>
                        <td>${user.is_active ? '<span class="badge badge-active">Active</span>' : '<span class="badge badge-inactive">Inactive</span>'}</td>
                        <td>${new Date(user.created_at).toLocaleDateString()}</td>
                        <td class="actions">
                            ${user.is_active
                                ? `<button class="btn" onclick="toggleActive(${user.id}, false)">Deactivate</button>`
                                : `<button class="btn" onclick="toggleActive(${user.id}, true)">Activate</button>`
                            }
                            <button class="btn btn-danger" onclick="deleteUser(${user.id}, '${user.username}')">Delete</button>
                        </td>
                    </tr>
                `).join('');
            } catch (error) {
                showMessage('Error loading users: ' + error.message, 'error');
            }
        }

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => msg.className = 'message', 3000);
        }

        async function updateRole(userId, role) {
            try {
                const response = await fetch(`/api/users/${userId}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: role})
                });
                if (response.ok) {
                    showMessage('Role updated', 'success');
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error updating role', 'error');
                    loadUsers();
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        }

        async function toggleActive(userId, isActive) {
            try {
                const response = await fetch(`/api/users/${userId}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({is_active: isActive})
                });
                if (response.ok) {
                    showMessage('User updated', 'success');
                    loadUsers();
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error updating user', 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        }

        async function deleteUser(userId, username) {
            if (!confirm(`Delete user "${username}"?`)) return;
            try {
                const response = await fetch(`/api/users/${userId}`, {method: 'DELETE'});
                if (response.ok) {
                    showMessage('User deleted', 'success');
                    loadUsers();
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error deleting user', 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        }

        document.getElementById('addUserForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('newUsername').value;
            const password = document.getElementById('newPassword').value;
            const role = document.getElementById('newRole').value;

            try {
                const response = await fetch('/api/users', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username, password, role: role})
                });

                if (response.ok) {
                    showMessage('User created', 'success');
                    document.getElementById('addUserForm').reset();
                    loadUsers();
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error creating user', 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        });

        loadUsers();
        loadRoleModes();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/prompt-library", response_class=HTMLResponse)
async def prompt_library_page(current_user: User = Depends(get_current_user)):
    """Serve the prompt library management page (Prompts). Skills are now filesystem Agent Skills."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot Prompt Library</title>
    <link rel="icon" type="image/png" href="/frontend/leonardo-icon.png">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {
            --bg-color: #0d0d1a;
            --chat-bg: #1a1730;
            --text-color: #e0e0e0;
            --border-color: rgba(139, 92, 246, 0.2);
            --accent-color: #8b5cf6;
            --accent-hover: #7c3aed;
            --skill-color: #3b82f6;
            --skill-hover: #2563eb;
        }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }
        .container {
            max-width: 1000px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        .header {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 20px;
        }
        .back-btn {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            text-decoration: none;
            transition: background 0.2s;
        }
        .back-btn:hover { background: rgba(139, 92, 246, 0.15); }
        h1 { font-size: 1.5rem; margin: 0; flex: 1; }
        /* Tab styles */
        .tab-container {
            display: flex;
            gap: 0;
            margin-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
        }
        .tab-btn {
            padding: 12px 24px;
            background: transparent;
            border: none;
            border-bottom: 3px solid transparent;
            color: rgba(255,255,255,0.6);
            cursor: pointer;
            font-size: 0.95rem;
            font-weight: 500;
            transition: all 0.2s;
        }
        .tab-btn:hover {
            color: var(--text-color);
            background: rgba(255,255,255,0.05);
        }
        .tab-btn.active {
            color: var(--accent-color);
            border-bottom-color: var(--accent-color);
        }
        .tab-btn.active.skill-tab {
            color: var(--skill-color);
            border-bottom-color: var(--skill-color);
        }
        .tab-btn i { margin-right: 8px; }
        .card {
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }
        .prompt-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 16px;
        }
        .prompt-card {
            background: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .prompt-card:hover {
            border-color: var(--accent-color);
            transform: translateY(-2px);
        }
        .prompt-name {
            font-weight: 600;
            margin-bottom: 8px;
            color: var(--text-color);
        }
        .prompt-description {
            font-size: 0.8rem;
            color: rgba(255,255,255,0.5);
            margin-bottom: 8px;
            font-style: italic;
        }
        .prompt-content {
            font-size: 0.85rem;
            color: rgba(255,255,255,0.6);
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .prompt-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 12px;
            font-size: 0.75rem;
            color: rgba(255,255,255,0.4);
        }
        .prompt-group-badge {
            background: rgba(139, 92, 246, 0.2);
            color: var(--accent-color);
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.7rem;
        }
        .btn {
            padding: 8px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85rem;
            background: transparent;
            color: var(--text-color);
            transition: all 0.2s;
        }
        .btn:hover { background: rgba(139, 92, 246, 0.15); }
        .btn-primary {
            background: linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%);
            border-color: #8b5cf6;
            color: white;
        }
        .btn-primary:hover { opacity: 0.9; }
        .btn-danger { border-color: rgba(239, 68, 68, 0.25); color: #ef4444; }
        .btn-danger:hover { background: rgba(239, 68, 68, 0.15); }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; margin-bottom: 6px; font-size: 0.85rem; color: rgba(255, 255, 255, 0.5); }
        .form-group input, .form-group select, .form-group textarea {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            box-sizing: border-box;
            font-family: inherit;
        }
        .form-group textarea { min-height: 150px; resize: vertical; }
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
            outline: none;
            border-color: rgba(139, 92, 246, 0.4);
            box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.08);
        }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: var(--chat-bg);
            border-radius: 12px;
            padding: 24px;
            max-width: 600px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .modal-header h2 { margin: 0; font-size: 1.2rem; }
        .modal-close {
            background: none;
            border: none;
            font-size: 1.5rem;
            cursor: pointer;
            color: var(--text-color);
            opacity: 0.6;
        }
        .modal-close:hover { opacity: 1; }
        .filter-bar {
            display: flex;
            gap: 12px;
            margin-bottom: 20px;
            flex-wrap: wrap;
            align-items: center;
        }
        .search-input {
            flex: 1;
            min-width: 200px;
            padding: 10px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
        }
        .search-input:focus { outline: none; border-color: var(--accent-color); }
        .group-select {
            padding: 10px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            min-width: 150px;
        }
        .empty-state {
            text-align: center;
            padding: 40px;
            color: rgba(255,255,255,0.5);
        }
        .empty-state i { font-size: 3rem; margin-bottom: 16px; opacity: 0.3; }
        .message {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: none;
        }
        .message.success { background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.25); color: #22c55e; display: block; }
        .message.error { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.25); color: #ef4444; display: block; }
        /* Skill-specific styles */
        .skill-card { border-left: 3px solid var(--skill-color); }
        .skill-card:hover { border-color: var(--skill-hover); }
        .skill-group-badge {
            background: rgba(59, 130, 246, 0.2);
            color: var(--skill-color);
        }
        .btn-skill {
            background: var(--skill-color);
            border-color: var(--skill-color);
        }
        .btn-skill:hover { background: var(--skill-hover); }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/" class="back-btn"><i class="fa-solid fa-arrow-left"></i></a>
            <h1>Prompt Library</h1>
        </div>

        <div class="tab-container">
            <button class="tab-btn active" data-tab="prompts" onclick="switchTab('prompts')">
                <i class="fa-solid fa-book"></i> Prompts
            </button>
        </div>

        <div id="message" class="message"></div>

        <!-- Prompts Tab -->
        <div class="tab-content active" id="promptsTab">
            <div class="filter-bar">
                <input type="text" class="search-input" placeholder="Search prompts..." id="promptSearchInput">
                <select class="group-select" id="promptGroupFilter">
                    <option value="">All Groups</option>
                </select>
                <button class="btn btn-primary" onclick="showCreatePromptModal()">
                    <i class="fa-solid fa-plus"></i> New Prompt
                </button>
            </div>
            <div class="prompt-grid" id="promptGrid">
                <div class="empty-state">
                    <i class="fa-solid fa-book"></i>
                    <p>Loading prompts...</p>
                </div>
            </div>
        </div>
    </div>

    <!-- Prompt Modal -->
    <div class="modal" id="promptModal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 id="promptModalTitle">New Prompt</h2>
                <button class="modal-close" onclick="closePromptModal()">&times;</button>
            </div>
            <form id="promptForm">
                <input type="hidden" id="promptId">
                <div class="form-group">
                    <label>Name *</label>
                    <input type="text" id="promptName" required placeholder="e.g., Code Review Instructions">
                </div>
                <div class="form-group">
                    <label>Group</label>
                    <input type="text" id="promptGroup" value="General" list="promptGroupSuggestions" placeholder="e.g., Code Review, Writing, Analysis">
                    <datalist id="promptGroupSuggestions"></datalist>
                </div>
                <div class="form-group">
                    <label>Description (optional)</label>
                    <input type="text" id="promptDescription" placeholder="Brief description of when to use this prompt">
                </div>
                <div class="form-group">
                    <label>Content * <span id="promptCharCount" style="float: right; font-weight: normal; color: #666;">0 / 50,000</span></label>
                    <textarea id="promptContent" required placeholder="Enter your prompt template..." oninput="updatePromptCharCount()"></textarea>
                </div>
                <div style="display: flex; gap: 12px; justify-content: flex-end;">
                    <button type="button" class="btn btn-danger" id="promptDeleteBtn" onclick="deletePrompt()" style="display: none; margin-right: auto;">Delete</button>
                    <button type="button" class="btn" onclick="closePromptModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        // State
        let activeTab = 'prompts';
        let prompts = [];
        let promptGroups = [];
        let currentPromptId = null;

        // Tab switching
        function switchTab(tab) {
            activeTab = tab;
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.tab === tab);
            });
            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.toggle('active', content.id === tab + 'Tab');
            });

            // Load data for the tab if not already loaded
            if (tab === 'prompts' && prompts.length === 0) {
                loadPrompts();
                loadPromptGroups();
            }
        }

        // Check URL for tab parameter
        const urlParams = new URLSearchParams(window.location.search);

        // ============== PROMPTS ==============

        async function loadPrompts() {
            const search = document.getElementById('promptSearchInput').value;
            const group = document.getElementById('promptGroupFilter').value;

            let url = '/api/prompts';
            const params = new URLSearchParams();
            if (search) params.set('search', search);
            if (group) params.set('group', group);
            if (params.toString()) url += '?' + params.toString();

            try {
                const response = await fetch(url);
                prompts = await response.json();
                renderPrompts();
            } catch (error) {
                showMessage('Error loading prompts: ' + error.message, 'error');
            }
        }

        async function loadPromptGroups() {
            try {
                const response = await fetch('/api/prompts/groups');
                const data = await response.json();
                promptGroups = data.groups;

                const select = document.getElementById('promptGroupFilter');
                const datalist = document.getElementById('promptGroupSuggestions');

                select.innerHTML = '<option value="">All Groups</option>';
                datalist.innerHTML = '';

                promptGroups.forEach(g => {
                    select.innerHTML += '<option value="' + escapeHtml(g) + '">' + escapeHtml(g) + '</option>';
                    datalist.innerHTML += '<option value="' + escapeHtml(g) + '">';
                });
            } catch (error) {
                console.error('Error loading prompt groups:', error);
            }
        }

        function renderPrompts() {
            const grid = document.getElementById('promptGrid');
            if (prompts.length === 0) {
                grid.innerHTML = '<div class="empty-state"><i class="fa-solid fa-book"></i><p>No prompts found. Create your first prompt!</p></div>';
                return;
            }

            grid.innerHTML = prompts.map(p => `
                <div class="prompt-card" onclick="editPrompt(${p.id})">
                    <div class="prompt-name">${escapeHtml(p.name)}</div>
                    ${p.description ? '<div class="prompt-description">' + escapeHtml(p.description) + '</div>' : ''}
                    <div class="prompt-content">${escapeHtml(p.content)}</div>
                    <div class="prompt-meta">
                        <span class="prompt-group-badge">${escapeHtml(p.group)}</span>
                        <span>Used ${p.usage_count} times</span>
                    </div>
                </div>
            `).join('');
        }

        function updatePromptCharCount() {
            const content = document.getElementById('promptContent').value;
            const count = content.length;
            const charCount = document.getElementById('promptCharCount');
            charCount.textContent = count.toLocaleString() + ' / 50,000';
            charCount.style.color = count > 50000 ? '#e57373' : (count > 40000 ? '#ffb74d' : '#666');
        }

        function showCreatePromptModal() {
            currentPromptId = null;
            document.getElementById('promptModalTitle').textContent = 'New Prompt';
            document.getElementById('promptId').value = '';
            document.getElementById('promptName').value = '';
            document.getElementById('promptGroup').value = 'General';
            document.getElementById('promptDescription').value = '';
            document.getElementById('promptContent').value = '';
            document.getElementById('promptDeleteBtn').style.display = 'none';
            document.getElementById('promptModal').classList.add('active');
            updatePromptCharCount();
        }

        function editPrompt(id) {
            const prompt = prompts.find(p => p.id === id);
            if (!prompt) return;

            currentPromptId = id;
            document.getElementById('promptModalTitle').textContent = 'Edit Prompt';
            document.getElementById('promptId').value = prompt.id;
            document.getElementById('promptName').value = prompt.name;
            document.getElementById('promptGroup').value = prompt.group;
            document.getElementById('promptDescription').value = prompt.description || '';
            document.getElementById('promptContent').value = prompt.content;
            document.getElementById('promptDeleteBtn').style.display = 'block';
            document.getElementById('promptModal').classList.add('active');
            updatePromptCharCount();
        }

        function closePromptModal() {
            document.getElementById('promptModal').classList.remove('active');
            currentPromptId = null;
        }

        async function deletePrompt() {
            if (!currentPromptId) return;
            if (!confirm('Delete this prompt?')) return;

            try {
                const response = await fetch('/api/prompts/' + currentPromptId, { method: 'DELETE' });
                if (response.ok) {
                    showMessage('Prompt deleted', 'success');
                    closePromptModal();
                    loadPrompts();
                    loadPromptGroups();
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error deleting prompt', 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        }

        document.getElementById('promptForm').addEventListener('submit', async (e) => {
            e.preventDefault();

            const id = document.getElementById('promptId').value;
            const content = document.getElementById('promptContent').value;

            if (content.length > 50000) {
                showMessage('Content is too long (' + content.length + ' chars). Max: 50,000 characters.', 'error');
                return;
            }

            const data = {
                name: document.getElementById('promptName').value,
                group: document.getElementById('promptGroup').value,
                description: document.getElementById('promptDescription').value,
                content: content
            };

            const url = id ? '/api/prompts/' + id : '/api/prompts';
            const method = id ? 'PATCH' : 'POST';

            try {
                const response = await fetch(url, {
                    method,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                if (response.ok) {
                    showMessage(id ? 'Prompt updated' : 'Prompt created', 'success');
                    closePromptModal();
                    loadPrompts();
                    loadPromptGroups();
                } else {
                    let errorMsg = 'Error saving prompt';
                    const contentType = response.headers.get('content-type');
                    if (contentType && contentType.includes('application/json')) {
                        const error = await response.json();
                        errorMsg = error.detail || errorMsg;
                    }
                    showMessage(errorMsg, 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        });

        // ============== COMMON ==============

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => msg.className = 'message', 3000);
        }

        // Search handlers
        let promptSearchTimeout;
        document.getElementById('promptSearchInput').addEventListener('input', () => {
            clearTimeout(promptSearchTimeout);
            promptSearchTimeout = setTimeout(loadPrompts, 300);
        });
        document.getElementById('promptGroupFilter').addEventListener('change', loadPrompts);

        // Close modals on outside click
        document.getElementById('promptModal').addEventListener('click', (e) => {
            if (e.target.id === 'promptModal') closePromptModal();
        });

        // Initial load
        loadPrompts();
        loadPromptGroups();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/settings/environment", response_class=HTMLResponse)
async def environment_page(current_user: User = Depends(get_current_user)):
    """Environment variable inventory, feature switches and the model list.

    A real HTML file rather than an f-string like the other pages here: the
    escaping cost of ``{{`` in a page this JavaScript-heavy buys nothing, and all
    of its data arrives from ``/api/env-vars`` anyway. The only thing injected is
    the admin flag, which shapes the UI — every endpoint re-checks the role
    server-side, so a tampered flag reveals nothing.
    """
    if not (current_user.role == "engineer" or current_user.is_admin):
        raise HTTPException(status_code=403, detail="Engineers or admins only")

    with open(frontend_dir / "environment.html") as f:
        html = f.read()

    flag = "true" if current_user.is_admin else "false"
    html = html.replace(
        "<script>", f"<script>window.LLAMABOT_IS_ADMIN = {flag};</script>\n<script>", 1
    )
    return HTMLResponse(content=html)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    """Serve the settings page."""
    from app.routers.api import get_site_setting
    show_token_wheel = get_site_setting(session, "show_token_wheel", "false") == "true"
    proactive_build = get_site_setting(session, "proactive_build_after_ticket", "false") == "true"
    vscode_on = get_site_setting(session, "enable_vscode", "false") == "true"
    browser_inspect_on = get_site_setting(session, "enable_browser_inspect", "false") == "true"
    live_browser_tools_on = get_site_setting(session, "enable_live_browser_tools", "false") == "true"
    is_engineer_or_admin = current_user.role == "engineer" or current_user.is_admin

    # Browser-tab rows. Built here rather than inside the page f-string because
    # the page body uses doubled braces for CSS, which a nested loop would make
    # unreadable. The App row is rendered first, checked and disabled, so the
    # rule ("App is always on") is visible rather than merely implied by absence.
    visible_tabs = resolve_visible_tabs(session)
    browser_tab_rows = """
            <div class="menu-item" style="cursor: default; opacity: 0.6;">
                <i class="fa-solid fa-window-restore"></i>
                <span>Your App</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;"
                    title="The App tab is always visible">
                    <input type="checkbox" checked disabled style="opacity: 0; width: 0; height: 0;">
                    <span style="position: absolute; top: 0; left: 0; right: 0; bottom: 0; background-color: #8b5cf6; border-radius: 24px;"></span>
                    <span style="position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transform: translateX(20px);"></span>
                </label>
            </div>
            <div style="padding: 4px 0 12px 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                Always shown &mdash; this is the only view of your own application
            </div>
"""
    for tab in OPTIONAL_BROWSER_TABS:
        on = tab["target"] in visible_tabs
        browser_tab_rows += f"""
            <div class="menu-item" style="cursor: default;">
                <i class="fa-solid {tab['icon']}"></i>
                <span>{escape(tab['label'])}</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;">
                    <input type="checkbox" class="browser-tab-toggle" data-target="{tab['target']}"
                        {'checked' if on else ''} style="opacity: 0; width: 0; height: 0;"
                        onchange="saveVisibleTabs()">
                    <span style="position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: {'#8b5cf6' if on else '#555'}; border-radius: 24px; transition: 0.3s;"></span>
                    <span style="position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transition: 0.3s; transform: translateX({'20px' if on else '0'});"></span>
                </label>
            </div>
            <div style="padding: 4px 0 12px 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                {escape(tab['description'])}
            </div>
"""

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot Settings</title>
    <link rel="icon" type="image/png" href="/frontend/leonardo-icon.png">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {{
            --bg-color: #0d0d1a;
            --chat-bg: #1a1730;
            --text-color: #e0e0e0;
            --border-color: rgba(139, 92, 246, 0.2);
            --accent-color: #8b5cf6;
        }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            padding: 40px 20px;
        }}
        .header {{
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 40px;
        }}
        .back-btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            text-decoration: none;
            transition: background 0.2s;
        }}
        .back-btn:hover {{
            background: rgba(139, 92, 246, 0.15);
        }}
        h1 {{
            font-size: 1.5rem;
            margin: 0;
        }}
        .card {{
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }}
        .card-header {{
            font-size: 0.85rem;
            color: rgba(255, 255, 255, 0.5);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
        }}
        .user-info {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .user-avatar {{
            width: 48px;
            height: 48px;
            background: var(--accent-color);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            font-weight: 600;
            color: white;
        }}
        .user-details h3 {{
            margin: 0 0 4px 0;
            font-size: 1.1rem;
        }}
        .user-details .role {{
            font-size: 0.85rem;
            color: rgba(255, 255, 255, 0.5);
        }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            background: rgba(139, 92, 246, 0.2);
            color: #a78bfa;
            border-radius: 4px;
            font-size: 0.75rem;
            margin-left: 8px;
        }}
        .menu-item {{
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 14px 0;
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            transition: opacity 0.2s;
        }}
        .menu-item:last-child {{
            border-bottom: none;
        }}
        .menu-item:hover {{
            opacity: 0.8;
        }}
        .menu-item i {{
            width: 24px;
            text-align: center;
            font-size: 16px;
        }}
        .menu-item span {{
            flex: 1;
        }}
        .menu-item .chevron {{
            color: rgba(255, 255, 255, 0.35);
        }}
        .logout-btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            width: 100%;
            padding: 14px;
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.25);
            border-radius: 8px;
            color: #ef4444;
            font-size: 1rem;
            cursor: pointer;
            transition: all 0.2s;
        }}
        .logout-btn:hover {{
            background: rgba(239, 68, 68, 0.25);
        }}
        a {{
            color: inherit;
            text-decoration: none;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/" class="back-btn">
                <i class="fa-solid fa-arrow-left"></i>
            </a>
            <h1>Settings</h1>
        </div>

        <div class="card">
            <div class="card-header">Account</div>
            <div class="user-info">
                <div class="user-avatar">{current_user.username[0].upper()}</div>
                <div class="user-details">
                    <h3>{current_user.username}{"<span class='badge'>Admin</span>" if current_user.is_admin else ""}</h3>
                    <div class="role">{"Administrator" if current_user.is_admin else "User"}</div>
                </div>
            </div>
        </div>

        {"<div class='card'><a href='/users' class='menu-item'><i class='fa-solid fa-users'></i><span>User Management</span><i class='fa-solid fa-chevron-right chevron'></i></a></div>" if current_user.is_admin else ""}

        <div class='card'><a href='/prompt-library' class='menu-item'><i class='fa-solid fa-book'></i><span>Prompt Library</span><i class='fa-solid fa-chevron-right chevron'></i></a></div>

        <div class='card'>
            <div class='card-header'>Project Context</div>
            <a href='/leonardo-md' class='menu-item'><i class='fa-solid fa-file-lines'></i><span>LEONARDO.md</span><i class='fa-solid fa-chevron-right chevron'></i></a>
        </div>

        <!-- ChatGPT account. Sign-in lives in a modal on the chat page; this card
             is how a user ever learns it exists, and how they see whether the
             connection is one that renews itself. It links there rather than
             duplicating the flow. -->
        <div class='card'>
            <div class='card-header'>ChatGPT Account</div>
            <a href='/?connect=chatgpt' class='menu-item'>
                <i class='fa-brands fa-openai'></i>
                <span id='chatgptAccountLabel'>Checking…</span>
                <i class='fa-solid fa-chevron-right chevron'></i>
            </a>
            <div id='chatgptAccountNote' style='padding: 4px 0 0 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);'>
                Run Leo on your own ChatGPT Plus/Pro plan for the models marked “my ChatGPT plan”.
            </div>
        </div>

        {"<div class='card'><div class='card-header'>Automation</div><a href='/scheduled-jobs' class='menu-item'><i class='fa-solid fa-clock'></i><span>Scheduled Jobs</span><i class='fa-solid fa-chevron-right chevron'></i></a></div>" if current_user.role == 'engineer' or current_user.is_admin else ""}

        {"<div class='card'><div class='card-header'>Configuration</div><a href='/settings/environment' class='menu-item'><i class='fa-solid fa-sliders'></i><span>Environment &amp; Models</span><i class='fa-solid fa-chevron-right chevron'></i></a></div>" if current_user.role == 'engineer' or current_user.is_admin else ""}

        <div class="card">
            <div class="card-header">Backup</div>
            <a href="/backup-history" target="_blank" class="menu-item">
                <i class="fa-solid fa-clock-rotate-left"></i>
                <span>Backup History</span>
                <i class="fa-solid fa-chevron-right chevron"></i>
            </a>
            <div class="menu-item" style="cursor: default;">
                <i class="fa-solid fa-cloud-arrow-up"></i>
                <span>Auto-backup on completion</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;">
                    <input type="checkbox" id="autoBackupToggle" style="opacity: 0; width: 0; height: 0;"
                        onchange="toggleAutoBackup(this.checked)">
                    <span style="position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #555; border-radius: 24px; transition: 0.3s;"></span>
                    <span id="autoBackupSlider" style="position: absolute; content: ''; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transition: 0.3s;"></span>
                </label>
            </div>
            <div style="padding: 4px 0 0 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                Runs cloud backup after each task completes
            </div>
        </div>

        <div class="card">
            <div class="card-header">App Preview</div>
            <div class="menu-item" style="cursor: default;">
                <i class="fa-solid fa-arrows-rotate"></i>
                <span>Auto-refresh on edits</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;">
                    <input type="checkbox" id="autoRefreshOnEditToggle" style="opacity: 0; width: 0; height: 0;"
                        onchange="toggleAutoRefreshOnEdit(this.checked)">
                    <span style="position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #555; border-radius: 24px; transition: 0.3s;"></span>
                    <span id="autoRefreshOnEditSlider" style="position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transition: 0.3s;"></span>
                </label>
            </div>
            <div style="padding: 4px 0 0 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                Reloads the app preview iframe after each edit_file or write_file tool call
            </div>
        </div>

        {"" if not is_engineer_or_admin else '''<div class="card">
            <div class="card-header">Display</div>
            <div class="menu-item" style="cursor: default;">
                <i class="fa-solid fa-chart-pie"></i>
                <span>Show Token Wheel</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;">
                    <input type="checkbox" id="tokenWheelToggle" style="opacity: 0; width: 0; height: 0;"
                        onchange="toggleTokenWheel(this.checked)">
                    <span style="position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #555; border-radius: 24px; transition: 0.3s;"></span>
                    <span id="tokenWheelSlider" style="position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transition: 0.3s;"></span>
                </label>
            </div>
            <div style="padding: 4px 0 0 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                Shows context window usage percentage in chat
            </div>
        </div>'''}

        {"" if not is_engineer_or_admin else f'''<div class="card">
            <div class="card-header">Code Editor</div>
            <div style="padding: 0 0 12px 0; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                The VS Code editor runs in its own container. It is off by default.
                Turning it on starts the container and shows the Code tab in chat.
                Turning it off stops the container and hides the Code tab.
                The editor stays off after a restart until you turn it on again.
            </div>
            <div class="menu-item" style="cursor: default;">
                <i class="fa-solid fa-code"></i>
                <span>Enable VS Code</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;">
                    <input type="checkbox" id="vscodeToggle" {'checked' if vscode_on else ''}
                        style="opacity: 0; width: 0; height: 0;" onchange="toggleVSCode(this.checked)">
                    <span style="position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: {'#8b5cf6' if vscode_on else '#555'}; border-radius: 24px; transition: 0.3s;"></span>
                    <span id="vscodeSlider" style="position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transition: 0.3s; transform: translateX({'20px' if vscode_on else '0'});"></span>
                </label>
            </div>
            <div id="vscodeStatus" style="padding: 4px 0 0 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                {'Editor is on. Reload the chat page to see the Code tab.' if vscode_on else 'Editor is off.'}
            </div>
        </div>'''}

        {"" if not is_engineer_or_admin else f'''<div class="card">
            <div class="card-header">Browser Tabs</div>
            <div style="padding: 0 0 12px 0; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                Which tabs appear above the app preview. Reload the chat page to see changes.
            </div>
            {browser_tab_rows}
        </div>'''}

        {"" if not is_engineer_or_admin else f'''<div class="card">
            <div class="card-header">Automation</div>
            <div class="menu-item" style="cursor: default;">
                <i class="fa-solid fa-bolt"></i>
                <span>Proactively Build After Ticket</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;">
                    <input type="checkbox" id="proactiveBuildToggle" style="opacity: 0; width: 0; height: 0;"
                        onchange="toggleProactiveBuild(this.checked)">
                    <span style="position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #555; border-radius: 24px; transition: 0.3s;"></span>
                    <span id="proactiveBuildSlider" style="position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transition: 0.3s;"></span>
                </label>
            </div>
            <div style="padding: 4px 0 0 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                Automatically switches to Engineer mode and starts building after a ticket is written
            </div>
        </div>'''}

        {"" if not is_engineer_or_admin else f'''<div class="card">
            <div class="card-header">Developer Tools</div>
            <div class="menu-item" style="cursor: default;">
                <i class="fa-solid fa-magnifying-glass-chart"></i>
                <span>Browser Inspect Tool</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;">
                    <input type="checkbox" id="browserInspectToggle" style="opacity: 0; width: 0; height: 0;"
                        onchange="toggleBrowserInspect(this.checked)">
                    <span style="position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #555; border-radius: 24px; transition: 0.3s;"></span>
                    <span id="browserInspectSlider" style="position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transition: 0.3s;"></span>
                </label>
            </div>
            <div style="padding: 4px 0 0 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                Lets the agent inspect live pages with a headless browser (console logs, DOM, screenshots). Takes effect after the next restart.
            </div>
            <div class="menu-item" style="cursor: default;">
                <i class="fa-solid fa-window-maximize"></i>
                <span>Live Browser Tools</span>
                <label style="position: relative; display: inline-block; width: 44px; height: 24px;">
                    <input type="checkbox" id="liveBrowserToolsToggle" style="opacity: 0; width: 0; height: 0;"
                        onchange="toggleLiveBrowserTools(this.checked)">
                    <span style="position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #555; border-radius: 24px; transition: 0.3s;"></span>
                    <span id="liveBrowserToolsSlider" style="position: absolute; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; border-radius: 50%; transition: 0.3s;"></span>
                </label>
            </div>
            <div style="padding: 4px 0 0 36px; font-size: 0.75rem; color: rgba(255,255,255,0.35);">
                Lets the agent navigate the user's live app preview, read its console logs, and run JavaScript in it. Takes effect after the next restart.
            </div>
        </div>'''}

        <div class="card">
            <button class="logout-btn" onclick="logout()">
                <i class="fa-solid fa-right-from-bracket"></i>
                Sign Out
            </button>
        </div>
    </div>

    <script>
        // ChatGPT connection status. Read-only here — the sign-in modal lives on
        // the chat page, so the row links there with ?connect=chatgpt.
        (async function() {{
            const label = document.getElementById('chatgptAccountLabel');
            const note = document.getElementById('chatgptAccountNote');
            if (!label) return;
            let status;
            try {{
                const r = await fetch('/api/chatgpt-auth/status');
                status = r.ok ? await r.json() : null;
            }} catch (_) {{ status = null; }}

            if (!status) {{ label.textContent = 'Connection status unavailable'; return; }}
            if (!status.connected) {{ label.textContent = 'Not connected — sign in'; return; }}

            const who = status.account_email || 'Connected';
            const plan = status.plan_tier ? ` (${{status.plan_tier}})` : '';
            label.textContent = who + plan;
            if (!note) return;
            if (status.can_auto_refresh) {{
                note.textContent = status.auth_method === 'device_code'
                    ? 'Signed in with a device code. The token renews automatically.'
                    : 'The token renews automatically.';
            }} else {{
                // Say this BEFORE it expires — otherwise Leo silently drops back
                // to the default model mid-task with no explanation.
                note.textContent = 'Connected with a pasted token, which cannot renew itself. Sign in again to switch to a device code.';
                note.style.color = '#f5c26b';
            }}
        }})();

        // Auto-backup toggle
        (function() {{
            const toggle = document.getElementById('autoBackupToggle');
            const slider = document.getElementById('autoBackupSlider');
            const isEnabled = localStorage.getItem('autoBackupEnabled') !== 'false';
            toggle.checked = isEnabled;
            updateSliderStyle(isEnabled);
        }})();

        function toggleAutoBackup(enabled) {{
            localStorage.setItem('autoBackupEnabled', enabled ? 'true' : 'false');
            updateSliderStyle(enabled);
        }}

        function updateSliderStyle(enabled) {{
            const slider = document.getElementById('autoBackupSlider');
            const track = slider.previousElementSibling;
            if (enabled) {{
                track.style.backgroundColor = '#8b5cf6';
                slider.style.transform = 'translateX(20px)';
            }} else {{
                track.style.backgroundColor = '#555';
                slider.style.transform = 'translateX(0)';
            }}
        }}

        // Auto-refresh on edit toggle (defaults to enabled)
        (function() {{
            const toggle = document.getElementById('autoRefreshOnEditToggle');
            if (!toggle) return;
            const isEnabled = localStorage.getItem('autoRefreshOnEdit') !== 'false';
            toggle.checked = isEnabled;
            updateAutoRefreshOnEditSlider(isEnabled);
        }})();

        function toggleAutoRefreshOnEdit(enabled) {{
            localStorage.setItem('autoRefreshOnEdit', enabled ? 'true' : 'false');
            updateAutoRefreshOnEditSlider(enabled);
        }}

        function updateAutoRefreshOnEditSlider(enabled) {{
            const slider = document.getElementById('autoRefreshOnEditSlider');
            if (!slider) return;
            const track = slider.previousElementSibling;
            if (enabled) {{
                track.style.backgroundColor = '#8b5cf6';
                slider.style.transform = 'translateX(20px)';
            }} else {{
                track.style.backgroundColor = '#555';
                slider.style.transform = 'translateX(0)';
            }}
        }}

        // Token wheel toggle
        (function() {{
            const toggle = document.getElementById('tokenWheelToggle');
            const slider = document.getElementById('tokenWheelSlider');
            if (!toggle || !slider) return;
            const isEnabled = {'true' if show_token_wheel else 'false'};
            toggle.checked = isEnabled;
            updateTokenWheelSlider(isEnabled);
        }})();

        function toggleTokenWheel(enabled) {{
            updateTokenWheelSlider(enabled);
            fetch('/api/site-settings/show_token_wheel', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ value: enabled ? 'true' : 'false' }})
            }});
        }}

        // Code editor. One request both writes the setting and starts or stops
        // the editor container, so the toggle is disabled while docker works.
        // Starting can take a while on the first run, because docker may have to
        // pull the editor image.
        function toggleVSCode(enabled) {{
            const toggle = document.getElementById('vscodeToggle');
            const status = document.getElementById('vscodeStatus');
            toggle.disabled = true;
            status.textContent = enabled ? 'Starting the editor…' : 'Stopping the editor…';

            fetch(enabled ? '/api/vscode/enable' : '/api/vscode/disable', {{ method: 'POST' }})
                .then(function (r) {{ return r.json(); }})
                .then(function (body) {{
                    // The server decides the final state. A failed start leaves
                    // the editor off, so the toggle follows `enabled` from the
                    // response, not the click.
                    toggle.checked = !!body.enabled;
                    updateVSCodeSlider(toggle.checked);
                    if (body.ok) {{
                        status.textContent = toggle.checked
                            ? 'Editor is on. Reload the chat page to see the Code tab.'
                            : 'Editor is off. Reload the chat page to hide the Code tab.';
                    }} else {{
                        status.textContent = 'Editor command failed: ' + (body.output || 'unknown error');
                    }}
                }})
                .catch(function (e) {{
                    status.textContent = 'Editor command failed: ' + e;
                }})
                .finally(function () {{ toggle.disabled = false; }});
        }}

        function updateVSCodeSlider(enabled) {{
            const slider = document.getElementById('vscodeSlider');
            if (!slider) return;
            const track = slider.previousElementSibling;
            track.style.backgroundColor = enabled ? '#8b5cf6' : '#555';
            slider.style.transform = enabled ? 'translateX(20px)' : 'translateX(0)';
        }}

        // Browser tab visibility. The whole set is written as one comma-separated
        // value on every change — sending the full list rather than a per-tab
        // delta means two quick toggles can't interleave into a half-applied set.
        function saveVisibleTabs() {{
            const toggles = Array.from(document.querySelectorAll('.browser-tab-toggle'));
            toggles.forEach(function (t) {{ updateBrowserTabSlider(t); }});

            const chosen = toggles.filter(function (t) {{ return t.checked; }})
                                  .map(function (t) {{ return t.dataset.target; }});

            fetch('/api/site-settings/visible_tabs', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ value: chosen.join(',') }})
            }});
        }}

        function updateBrowserTabSlider(toggle) {{
            const track = toggle.nextElementSibling;
            const knob = track && track.nextElementSibling;
            if (!track || !knob) return;
            track.style.backgroundColor = toggle.checked ? '#8b5cf6' : '#555';
            knob.style.transform = toggle.checked ? 'translateX(20px)' : 'translateX(0)';
        }}

        function updateTokenWheelSlider(enabled) {{
            const slider = document.getElementById('tokenWheelSlider');
            if (!slider) return;
            const track = slider.previousElementSibling;
            if (enabled) {{
                track.style.backgroundColor = '#8b5cf6';
                slider.style.transform = 'translateX(20px)';
            }} else {{
                track.style.backgroundColor = '#555';
                slider.style.transform = 'translateX(0)';
            }}
        }}

        // Proactive build toggle
        (function() {{
            const toggle = document.getElementById('proactiveBuildToggle');
            const slider = document.getElementById('proactiveBuildSlider');
            if (!toggle || !slider) return;
            const isEnabled = {'true' if proactive_build else 'false'};
            toggle.checked = isEnabled;
            updateProactiveBuildSlider(isEnabled);
        }})();

        function toggleProactiveBuild(enabled) {{
            updateProactiveBuildSlider(enabled);
            fetch('/api/site-settings/proactive_build_after_ticket', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ value: enabled ? 'true' : 'false' }})
            }});
        }}

        function updateProactiveBuildSlider(enabled) {{
            const slider = document.getElementById('proactiveBuildSlider');
            if (!slider) return;
            const track = slider.previousElementSibling;
            if (enabled) {{
                track.style.backgroundColor = '#8b5cf6';
                slider.style.transform = 'translateX(20px)';
            }} else {{
                track.style.backgroundColor = '#555';
                slider.style.transform = 'translateX(0)';
            }}
        }}

        // Browser inspect toggle
        (function() {{
            const toggle = document.getElementById('browserInspectToggle');
            const slider = document.getElementById('browserInspectSlider');
            if (!toggle || !slider) return;
            const isEnabled = {'true' if browser_inspect_on else 'false'};
            toggle.checked = isEnabled;
            updateBrowserInspectSlider(isEnabled);
        }})();

        function toggleBrowserInspect(enabled) {{
            updateBrowserInspectSlider(enabled);
            fetch('/api/site-settings/enable_browser_inspect', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ value: enabled ? 'true' : 'false' }})
            }});
        }}

        function updateBrowserInspectSlider(enabled) {{
            const slider = document.getElementById('browserInspectSlider');
            if (!slider) return;
            const track = slider.previousElementSibling;
            if (enabled) {{
                track.style.backgroundColor = '#8b5cf6';
                slider.style.transform = 'translateX(20px)';
            }} else {{
                track.style.backgroundColor = '#555';
                slider.style.transform = 'translateX(0)';
            }}
        }}

        // Live browser tools toggle
        (function() {{
            const toggle = document.getElementById('liveBrowserToolsToggle');
            const slider = document.getElementById('liveBrowserToolsSlider');
            if (!toggle || !slider) return;
            const isEnabled = {'true' if live_browser_tools_on else 'false'};
            toggle.checked = isEnabled;
            updateLiveBrowserToolsSlider(isEnabled);
        }})();

        function toggleLiveBrowserTools(enabled) {{
            updateLiveBrowserToolsSlider(enabled);
            fetch('/api/site-settings/enable_live_browser_tools', {{
                method: 'PUT',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ value: enabled ? 'true' : 'false' }})
            }});
        }}

        function updateLiveBrowserToolsSlider(enabled) {{
            const slider = document.getElementById('liveBrowserToolsSlider');
            if (!slider) return;
            const track = slider.previousElementSibling;
            if (enabled) {{
                track.style.backgroundColor = '#8b5cf6';
                slider.style.transform = 'translateX(20px)';
            }} else {{
                track.style.backgroundColor = '#555';
                slider.style.transform = 'translateX(0)';
            }}
        }}

        function logout() {{
            // Plain navigation: the server clears the cookie and redirects to
            // the sign-in form. Deliberately not a fetch — a 401 response to a
            // fetch triggers the browser's native auth dialog.
            window.location.href = '/logout';
        }}
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.post("/logout")
async def logout():
    """Logout endpoint.

    Clears the session cookie *and* returns 401 with WWW-Authenticate: Basic
    so any browser still relying on cached Basic creds also gets evicted.
    Both auth modes are cleared in one shot.
    """
    response = JSONResponse(
        {"detail": "Logged out"},
        status_code=401,
        headers={"WWW-Authenticate": "Basic"},
    )
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/logout")
async def logout_get():
    """Browser logout — clear the session cookie and land on the sign-in form.

    Unlike POST /logout this sends no WWW-Authenticate challenge: a 401 on a
    fetch()/navigation is what makes the browser throw up its native Basic Auth
    dialog, which is exactly what we don't want when the user clicks Sign Out.
    Scripted callers that want cached Basic creds evicted still use POST.
    """
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/leonardo-md", response_class=HTMLResponse)
async def leonardo_md_page(current_user: User = Depends(get_current_user)):
    """Serve the LEONARDO.md editor page."""
    can_edit = current_user.is_admin or getattr(current_user, 'role', 'user') == "engineer"

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot - LEONARDO.md</title>
    <link rel="icon" type="image/png" href="/frontend/leonardo-icon.png">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {{
            --bg-color: #0d0d1a;
            --chat-bg: #1a1730;
            --text-color: #e0e0e0;
            --border-color: rgba(139, 92, 246, 0.2);
            --accent-color: #8b5cf6;
        }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }}
        .container {{
            max-width: 900px;
            margin: 0 auto;
            padding: 40px 20px;
        }}
        .header {{
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 30px;
        }}
        .back-btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            text-decoration: none;
            transition: background 0.2s;
        }}
        .back-btn:hover {{
            background: rgba(139, 92, 246, 0.15);
        }}
        h1 {{
            font-size: 1.5rem;
            margin: 0;
        }}
        .card {{
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }}
        .card-header {{
            font-size: 0.85rem;
            color: rgba(255, 255, 255, 0.5);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .card-header-note {{
            font-size: 0.75rem;
            color: rgba(255, 255, 255, 0.35);
            text-transform: none;
            letter-spacing: normal;
        }}
        textarea {{
            width: 100%;
            min-height: 400px;
            padding: 16px;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            background: var(--bg-color);
            color: var(--text-color);
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 14px;
            line-height: 1.6;
            resize: vertical;
            box-sizing: border-box;
        }}
        textarea:focus {{
            outline: none;
            border-color: rgba(139, 92, 246, 0.4);
            box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.08);
        }}
        textarea:read-only {{
            opacity: 0.7;
            cursor: not-allowed;
        }}
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-size: 0.95rem;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s;
        }}
        .btn-primary {{
            background: linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%);
            color: white;
        }}
        .btn-primary:hover {{
            opacity: 0.9;
        }}
        .btn-primary:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        .actions {{
            margin-top: 16px;
            display: flex;
            justify-content: flex-end;
            gap: 12px;
        }}
        .message {{
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 16px;
            display: none;
        }}
        .message.success {{
            display: block;
            background: rgba(34, 197, 94, 0.1);
            color: #22c55e;
            border: 1px solid rgba(34, 197, 94, 0.25);
        }}
        .message.error {{
            display: block;
            background: rgba(239, 68, 68, 0.1);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.25);
        }}
        .read-only-notice {{
            color: rgba(255, 255, 255, 0.5);
            font-size: 0.85rem;
            margin-top: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/settings" class="back-btn">
                <i class="fa-solid fa-arrow-left"></i>
            </a>
            <h1>LEONARDO.md</h1>
        </div>

        <div id="message" class="message"></div>

        <div class="card">
            <div class="card-header">
                Project Context
                <span class="card-header-note">- Appended to all Leonardo agent system prompts</span>
            </div>
            <textarea id="content" placeholder="# Project Context

Add project-specific instructions here...

## About This Project
Describe what this application does and the problem it solves.

## Technical Details
- Framework: Ruby on Rails
- Database: PostgreSQL
- Key gems or dependencies

## Coding Conventions
- Any specific patterns to follow
- Naming conventions
- Testing requirements

## Important Notes
- Business rules to keep in mind
- Edge cases to handle
" {'readonly' if not can_edit else ''}></textarea>
            {'<div class="actions"><button class="btn btn-primary" onclick="saveContent()" id="saveBtn"><i class="fa-solid fa-save"></i> Save</button></div>' if can_edit else '<p class="read-only-notice"><i class="fa-solid fa-lock"></i> View only - engineer or admin role required to edit</p>'}
        </div>
    </div>

    <script>
        async function loadContent() {{
            try {{
                const response = await fetch('/api/leonardo-md');
                const data = await response.json();
                if (data.content !== null) {{
                    document.getElementById('content').value = data.content;
                }}
            }} catch (error) {{
                showMessage('Error loading content: ' + error.message, 'error');
            }}
        }}

        async function saveContent() {{
            const btn = document.getElementById('saveBtn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';

            const content = document.getElementById('content').value;
            try {{
                const response = await fetch('/api/leonardo-md', {{
                    method: 'PUT',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{content: content}})
                }});
                if (response.ok) {{
                    showMessage('Saved successfully!', 'success');
                }} else {{
                    const error = await response.json();
                    showMessage(error.detail || 'Error saving', 'error');
                }}
            }} catch (error) {{
                showMessage('Error: ' + error.message, 'error');
            }} finally {{
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-save"></i> Save';
            }}
        }}

        function showMessage(text, type) {{
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => {{ msg.className = 'message'; }}, 4000);
        }}

        // Load content on page load
        loadContent();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/conversations", response_class=HTMLResponse)
async def conversations(username: str = Depends(auth)):
    with open("conversations.html") as f:
        return f.read()


@router.get("/git-history", response_class=HTMLResponse)
async def git_history_page(current_user: User = Depends(get_current_user)):
    """Serve the full-page git history visualization."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Git History - LlamaBot</title>
    <link rel="icon" type="image/png" href="/frontend/leonardo-icon.png">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {
            --bg-color: #0d0d1a;
            --panel-bg: #1a1730;
            --text-color: #e0e0e0;
            --text-secondary: rgba(255, 255, 255, 0.5);
            --border-color: rgba(139, 92, 246, 0.2);
            --accent-color: #8b5cf6;
            --accent-hover: #7c3aed;
        }
        * { box-sizing: border-box; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .header {
            display: flex;
            align-items: center;
            gap: 16px;
            padding: 16px 24px;
            background: var(--panel-bg);
            border-bottom: 1px solid var(--border-color);
        }
        .back-btn {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            background: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            text-decoration: none;
            transition: all 0.2s;
        }
        .back-btn:hover { background: rgba(139, 92, 246, 0.15); }
        h1 { font-size: 1.3rem; margin: 0; flex: 1; }
        .header-actions { display: flex; gap: 8px; }
        .btn {
            padding: 8px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85rem;
            background: transparent;
            color: var(--text-color);
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .btn:hover { background: rgba(139, 92, 246, 0.15); }
        .btn-primary {
            background: linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%);
            border-color: #8b5cf6;
            color: white;
        }
        .btn-primary:hover { background: var(--accent-hover); }

        .main-content {
            flex: 1;
            display: flex;
            overflow: hidden;
            position: relative;
        }

        /* Branch legend - left sidebar */
        .branch-legend {
            width: 180px;
            flex-shrink: 0;
            background: var(--panel-bg);
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            height: calc(100vh - 73px);
        }

        .legend-header {
            padding: 12px 16px;
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-color);
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .legend-header i { color: var(--accent-color); }

        .legend-list {
            flex: 1;
            overflow-y: auto;
            padding: 8px 0;
        }

        .legend-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 16px;
            cursor: pointer;
            transition: background 0.15s;
        }
        .legend-item:hover { background: rgba(139, 92, 246, 0.1); }

        .legend-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            flex-shrink: 0;
        }

        .legend-name {
            font-size: 0.8rem;
            color: var(--text-color);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .legend-name.head {
            font-weight: 600;
            color: var(--accent-color);
        }

        /* Graph panel - left side, scrollable */
        .graph-panel {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            min-width: 0;
            height: calc(100vh - 73px); /* Full height minus header */
        }

        .graph-toolbar {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px 20px;
            background: var(--panel-bg);
            border-bottom: 1px solid var(--border-color);
        }

        .search-box {
            flex: 1;
            max-width: 300px;
            padding: 8px 12px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            font-size: 0.9rem;
        }
        .search-box:focus {
            outline: none;
            border-color: var(--accent-color);
        }

        .branch-filter {
            padding: 8px 12px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            font-size: 0.9rem;
            min-width: 150px;
        }

        .commit-count {
            font-size: 0.85rem;
            color: var(--text-secondary);
        }

        .graph-container {
            flex: 1;
            overflow-y: auto;
            padding: 0;
        }

        /* Commit list with SVG graph overlay */
        .commit-list-wrapper {
            display: flex;
            position: relative;
        }

        .git-graph-container {
            flex-shrink: 0;
            position: sticky;
            left: 0;
            z-index: 1;
            background: var(--panel-bg);
        }

        .git-graph-svg {
            display: block;
        }

        .git-graph-svg .svg-commit-node {
            cursor: pointer;
            transition: r 0.15s ease;
        }

        .git-graph-svg .svg-commit-node:hover {
            r: 8;
        }

        .commit-list {
            flex: 1;
            display: flex;
            flex-direction: column;
        }

        .commit-row {
            display: flex;
            align-items: stretch;
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            transition: background 0.15s;
            min-height: 52px;
            height: 52px; /* Fixed height to match SVG rowHeight */
        }
        .commit-row:hover { background: rgba(139, 92, 246, 0.1); }
        .commit-row.selected { background: rgba(139, 92, 246, 0.2); }
        .commit-row.current { background: rgba(139, 92, 246, 0.15); }

        .commit-info {
            flex: 1;
            padding: 8px 12px;
            min-width: 0;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }

        .commit-message {
            font-size: 0.9rem;
            color: var(--text-color);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-bottom: 4px;
        }

        .commit-meta {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 0.75rem;
            color: var(--text-secondary);
        }

        .commit-sha {
            font-family: 'Monaco', 'Menlo', monospace;
            background: rgba(139, 92, 246, 0.15);
            color: var(--accent-color);
            padding: 2px 6px;
            border-radius: 3px;
            cursor: pointer;
        }
        .commit-sha:hover { background: rgba(139, 92, 246, 0.3); }

        .commit-refs {
            display: flex;
            gap: 4px;
            flex-wrap: wrap;
        }

        .ref-badge {
            font-size: 0.65rem;
            padding: 2px 6px;
            border-radius: 3px;
            background: rgba(34, 197, 94, 0.2);
            color: #22c55e;
            white-space: nowrap;
        }
        .ref-badge.head { background: rgba(139, 92, 246, 0.2); color: #8b5cf6; }

        .commit-date {
            min-width: 100px;
            text-align: right;
            padding-right: 16px;
        }

        .commit-author {
            min-width: 120px;
        }

        /* Detail panel - right side, fixed position */
        .detail-panel {
            width: 340px;
            flex-shrink: 0;
            background: var(--panel-bg);
            border-left: 1px solid var(--border-color);
            position: sticky;
            top: 0;
            height: calc(100vh - 73px); /* Subtract header height */
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .detail-panel.hidden { display: none; }

        .detail-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            border-bottom: 1px solid var(--border-color);
            flex-shrink: 0;
        }

        .detail-sha {
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.85rem;
            background: rgba(139, 92, 246, 0.2);
            color: var(--accent-color);
            padding: 4px 10px;
            border-radius: 4px;
            cursor: pointer;
        }
        .detail-sha:hover { background: rgba(139, 92, 246, 0.4); }

        .detail-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.1rem;
            cursor: pointer;
            padding: 4px 8px;
        }
        .detail-close:hover { color: var(--text-color); }

        /* Actions right below header for easy access */
        .detail-actions {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            gap: 6px;
            flex-shrink: 0;
        }

        .detail-actions .btn {
            width: 100%;
            justify-content: center;
            padding: 6px 12px;
            font-size: 0.8rem;
        }

        .detail-body {
            flex: 1;
            overflow-y: auto;
            padding: 12px 16px;
        }

        .detail-message {
            font-size: 0.9rem;
            line-height: 1.5;
            color: var(--text-color);
            margin-bottom: 12px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border-color);
            white-space: pre-wrap;
            word-break: break-word;
        }

        .detail-meta {
            margin-bottom: 12px;
        }

        .detail-meta-row {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }
        .detail-meta-row i { width: 14px; color: var(--accent-color); font-size: 0.75rem; }

        .detail-files {
            background: var(--bg-color);
            border-radius: 6px;
            padding: 12px;
        }

        .detail-files-header {
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--text-color);
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .detail-files-header i { color: var(--accent-color); font-size: 0.75rem; }

        .detail-files-list {
            max-height: 200px;
            overflow-y: auto;
        }

        .file-item {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 4px 6px;
            font-size: 0.75rem;
            color: var(--text-secondary);
            border-radius: 3px;
        }
        .file-item:hover { background: rgba(255,255,255,0.05); }
        .file-item i { color: var(--accent-color); font-size: 0.65rem; }
        .file-name {
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.7rem;
            word-break: break-all;
        }

        /* Empty state */
        .empty-state {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 60px 20px;
            color: var(--text-secondary);
        }
        .empty-state i { font-size: 48px; margin-bottom: 16px; opacity: 0.5; }

        /* Loading state */
        .loading {
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 40px;
            color: var(--text-secondary);
        }
        .loading i { margin-right: 8px; }

        /* Toast notification */
        .toast {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #22c55e;
            color: white;
            padding: 12px 20px;
            border-radius: 8px;
            font-size: 0.9rem;
            opacity: 0;
            transform: translateY(20px);
            transition: all 0.3s;
            z-index: 1000;
        }
        .toast.show { opacity: 1; transform: translateY(0); }

        /* Branch colors */
        .branch-0 { --branch-color: #8b5cf6; }
        .branch-1 { --branch-color: #22c55e; }
        .branch-2 { --branch-color: #eab308; }
        .branch-3 { --branch-color: #3b82f6; }
        .branch-4 { --branch-color: #ef4444; }
        .branch-5 { --branch-color: #ec4899; }
        .branch-6 { --branch-color: #14b8a6; }
    </style>
</head>
<body>
    <div class="header">
        <a href="/" class="back-btn"><i class="fa-solid fa-arrow-left"></i></a>
        <h1><i class="fa-solid fa-code-branch"></i> Git History</h1>
        <div class="header-actions">
            <button class="btn" onclick="refreshData()">
                <i class="fa-solid fa-refresh"></i> Refresh
            </button>
            <button class="btn btn-primary" onclick="pushToRemote()">
                <i class="fa-solid fa-cloud-arrow-up"></i> Push
            </button>
        </div>
    </div>

    <div class="main-content">
        <div class="branch-legend" id="branchLegend">
            <div class="legend-header"><i class="fa-solid fa-code-branch"></i> Branches</div>
            <div class="legend-list" id="legendList"></div>
        </div>
        <div class="graph-panel">
            <div class="graph-toolbar">
                <input type="text" class="search-box" placeholder="Search commits..." id="searchInput">
                <select class="branch-filter" id="branchFilter">
                    <option value="">All branches</option>
                </select>
                <span class="commit-count" id="commitCount">Loading...</span>
            </div>
            <div class="graph-container" id="graphContainer">
                <div class="loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading commits...</div>
            </div>
        </div>

        <div class="detail-panel hidden" id="detailPanel">
            <div class="detail-header">
                <span class="detail-sha" id="detailSha" onclick="copySha()"></span>
                <button class="detail-close" onclick="closeDetail()"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <div class="detail-actions">
                <button class="btn btn-primary" onclick="rollbackTo()"><i class="fa-solid fa-rotate-left"></i> Rollback to this commit</button>
                <button class="btn" onclick="viewDiff()"><i class="fa-solid fa-code-compare"></i> View Full Diff</button>
            </div>
            <div class="detail-body">
                <div class="detail-message" id="detailMessage"></div>
                <div class="detail-meta" id="detailMeta"></div>
                <div class="detail-files">
                    <div class="detail-files-header"><i class="fa-regular fa-file-code"></i> Changed Files</div>
                    <div class="detail-files-list" id="detailFiles"></div>
                </div>
            </div>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        let commits = [];
        let branches = [];
        let maxBranchIndex = 0;
        let selectedCommit = null;
        let currentHead = null;
        const branchColors = ['#8b5cf6', '#22c55e', '#eab308', '#3b82f6', '#ef4444', '#ec4899', '#14b8a6'];
        const laneWidth = 20;

        async function loadData() {
            try {
                const [graphRes, headRes] = await Promise.all([
                    fetch('/api/checkpoints/git-graph?limit=100', { credentials: 'same-origin' }),
                    fetch('/api/checkpoints/current-head', { credentials: 'same-origin' })
                ]);

                const graphData = await graphRes.json();
                commits = graphData.commits || [];
                branches = graphData.branches || [];
                maxBranchIndex = graphData.max_branch_index || 0;

                const headData = await headRes.json();
                currentHead = headData.head_sha;

                renderBranchFilter();
                renderBranchLegend();
                renderCommits();
                document.getElementById('commitCount').textContent = commits.length + ' commits';
            } catch (e) {
                document.getElementById('graphContainer').innerHTML =
                    '<div class="empty-state"><i class="fa-solid fa-exclamation-triangle"></i><p>Failed to load commits</p></div>';
            }
        }

        function renderBranchFilter() {
            const select = document.getElementById('branchFilter');
            select.innerHTML = '<option value="">All branches</option>';
            branches.forEach(b => {
                select.innerHTML += '<option value="' + escapeHtml(b.name) + '">' + escapeHtml(b.name) + '</option>';
            });
        }

        function renderBranchLegend() {
            const legendList = document.getElementById('legendList');

            // Collect unique branches with their lane colors
            const branchMap = new Map();
            commits.forEach(c => {
                c.refs.forEach(ref => {
                    if (!ref.startsWith('origin/') && ref !== 'HEAD' && !branchMap.has(ref)) {
                        branchMap.set(ref, c.branch_index);
                    }
                });
            });

            // Also add HEAD if present
            const headCommit = commits.find(c => c.refs.includes('HEAD'));
            if (headCommit) {
                branchMap.set('HEAD', headCommit.branch_index);
            }

            if (branchMap.size === 0) {
                legendList.innerHTML = '<div style="padding:16px;color:var(--text-secondary);font-size:0.8rem;">No branches found</div>';
                return;
            }

            legendList.innerHTML = Array.from(branchMap.entries()).map(([name, laneIdx]) => {
                const color = branchColors[laneIdx % branchColors.length];
                const isHead = name === 'HEAD';
                return '<div class="legend-item" onclick="filterByBranch(\\'' + escapeHtml(name) + '\\')">' +
                    '<div class="legend-dot" style="background:' + color + ';"></div>' +
                    '<span class="legend-name' + (isHead ? ' head' : '') + '">' + escapeHtml(name) + '</span>' +
                '</div>';
            }).join('');
        }

        function filterByBranch(branchName) {
            const select = document.getElementById('branchFilter');
            // Find and select the branch, or search for it
            document.getElementById('searchInput').value = branchName;
            renderCommits();
        }

        // Build column segments for SVG rendering (DoltHub algorithm)
        // Each segment represents a continuous vertical line from start row to end row
        function buildSegments(commits) {
            const commitMap = new Map();
            commits.forEach((c, i) => commitMap.set(c.sha, { ...c, row: i }));
            const columns = [];

            commits.forEach((commit, row) => {
                const col = commit.branch_index;
                while (columns.length <= col) columns.push([]);

                // Does this commit's first parent live in the same column?
                const firstParent = commit.parent_shas && commit.parent_shas[0];
                const parentData = firstParent ? commitMap.get(firstParent) : null;
                const continuesSameColumn = parentData && parentData.branch_index === col;

                // Find an existing segment in this column that ends at this row
                const existing = columns[col].find(s => s.end === row);

                if (existing) {
                    // Extend to first parent if same column
                    if (continuesSameColumn) existing.end = parentData.row;
                } else {
                    // New segment: extends to parent if same column, else just this row
                    const end = continuesSameColumn ? parentData.row : row;
                    columns[col].push({ start: row, end });
                }
            });

            // Debug: log segments to verify algorithm
            console.log('Segments:', columns.map((segs, col) =>
                'Column ' + col + ': ' + segs.map(s => '[' + s.start + '→' + s.end + ']').join(', ')
            ));

            return columns;
        }

        // Render graph as a single SVG element
        function renderGraphSVG(commits, columns, rowHeight, laneWidth) {
            if (commits.length === 0) return '';

            const width = Math.max(80, (columns.length + 1) * laneWidth + 20);
            const height = commits.length * rowHeight;

            let paths = '';

            // Draw vertical lines from segments
            columns.forEach((segs, col) => {
                const x = col * laneWidth + 20;
                const color = branchColors[col % branchColors.length];

                segs.forEach(seg => {
                    const y1 = seg.start * rowHeight + rowHeight / 2;
                    const y2 = seg.end * rowHeight + rowHeight / 2;
                    paths += '<line x1="' + x + '" y1="' + y1 + '" x2="' + x + '" y2="' + y2 + '" stroke="' + color + '" stroke-width="2"/>';
                });
            });

            // Build commit index map for curve rendering
            const commitMap = new Map();
            commits.forEach((c, i) => commitMap.set(c.sha, { ...c, row: i }));

            // Helper: create right-angle path with rounded corners (SourceTree style)
            function crossColumnPath(x1, y1, x2, y2, radius) {
                radius = radius || 6;
                // Midpoint Y: horizontal segment runs close below the child (0.25 = tight, 0.5 = halfway)
                var midY = y1 + (y2 - y1) * 0.25;
                var dx = x2 > x1 ? 1 : -1;  // direction: right (+1) or left (-1)

                // Clamp radius so it doesn't exceed available space
                var r = Math.min(radius, Math.abs(x2 - x1), Math.abs(midY - y1), Math.abs(y2 - midY));

                // Path: vertical down, arc, horizontal, arc, vertical down
                return 'M ' + x1 + ' ' + y1 +
                    ' L ' + x1 + ' ' + (midY - r) +
                    ' A ' + r + ' ' + r + ' 0 0 ' + (dx > 0 ? 1 : 0) + ' ' + (x1 + r * dx) + ' ' + midY +
                    ' L ' + (x2 - r * dx) + ' ' + midY +
                    ' A ' + r + ' ' + r + ' 0 0 ' + (dx > 0 ? 0 : 1) + ' ' + x2 + ' ' + (midY + r) +
                    ' L ' + x2 + ' ' + y2;
            }

            // Draw cross-column connectors with 90-degree angles (SourceTree style)
            commits.forEach((commit, row) => {
                if (!commit.parent_shas) return;

                commit.parent_shas.forEach(parentSha => {
                    const parentData = commitMap.get(parentSha);
                    if (!parentData) return;

                    // Only draw connector if cross-column; same-column handled by vertical segments
                    if (parentData.branch_index === commit.branch_index) return;

                    const x1 = commit.branch_index * laneWidth + 20;
                    const y1 = row * rowHeight + rowHeight / 2;
                    const x2 = parentData.branch_index * laneWidth + 20;
                    const y2 = parentData.row * rowHeight + rowHeight / 2;

                    const color = branchColors[parentData.branch_index % branchColors.length];
                    paths += '<path d="' + crossColumnPath(x1, y1, x2, y2, 6) + '" stroke="' + color + '" stroke-width="2" fill="none"/>';
                });
            });

            // Draw commit nodes
            commits.forEach((commit, row) => {
                const x = commit.branch_index * laneWidth + 20;
                const y = row * rowHeight + rowHeight / 2;
                const color = branchColors[commit.branch_index % branchColors.length];
                const r = commit.is_merge ? 6 : 5;

                paths += '<circle cx="' + x + '" cy="' + y + '" r="' + r + '" fill="' + color + '" class="svg-commit-node" data-sha="' + commit.sha + '" data-short-sha="' + commit.short_sha + '"/>';
            });

            return '<svg width="' + width + '" height="' + height + '" class="git-graph-svg">' + paths + '</svg>';
        }

        // Render a single commit row (without graph - graph is handled by SVG overlay)
        function renderCommitRow(commit, idx) {
            const isCurrent = currentHead && commit.sha.startsWith(currentHead);
            const isSelected = selectedCommit && selectedCommit.sha === commit.sha;

            const refsHtml = commit.refs.slice(0, 3).map(ref => {
                const cls = ref === 'HEAD' || ref.includes('HEAD') ? 'ref-badge head' : 'ref-badge';
                return '<span class="' + cls + '">' + escapeHtml(ref) + '</span>';
            }).join('');

            return '<div class="commit-row' + (isCurrent ? ' current' : '') + (isSelected ? ' selected' : '') + '" data-sha="' + commit.sha + '" onclick="selectCommit(\\'' + commit.sha + '\\')">' +
                '<div class="commit-info">' +
                    '<div class="commit-message">' + escapeHtml(commit.subject) + '</div>' +
                    '<div class="commit-meta">' +
                        '<span class="commit-sha" onclick="event.stopPropagation(); copyShaQuick(\\'' + commit.short_sha + '\\')">' + commit.short_sha + '</span>' +
                        '<div class="commit-refs">' + refsHtml + '</div>' +
                        '<span class="commit-author"><i class="fa-regular fa-user"></i> ' + escapeHtml(commit.author) + '</span>' +
                        '<span class="commit-date">' + formatTime(commit.timestamp) + '</span>' +
                    '</div>' +
                '</div>' +
            '</div>';
        }

        function renderCommits() {
            const container = document.getElementById('graphContainer');
            const search = document.getElementById('searchInput').value.toLowerCase();
            const branchFilter = document.getElementById('branchFilter').value;

            let filtered = commits;
            if (search) {
                filtered = filtered.filter(c =>
                    c.subject.toLowerCase().includes(search) ||
                    c.sha.includes(search) ||
                    c.author.toLowerCase().includes(search)
                );
            }

            if (filtered.length === 0) {
                container.innerHTML = '<div class="empty-state"><i class="fa-solid fa-code-branch"></i><p>No commits found</p></div>';
                return;
            }

            const rowHeight = 52; // Must match CSS .commit-row min-height
            const columns = buildSegments(filtered);
            const graphSVG = renderGraphSVG(filtered, columns, rowHeight, laneWidth);
            const graphWidth = Math.max(80, (columns.length + 1) * laneWidth + 20);

            container.innerHTML = '<div class="commit-list-wrapper">' +
                '<div class="git-graph-container" style="width:' + graphWidth + 'px;">' + graphSVG + '</div>' +
                '<div class="commit-list">' +
                    filtered.map((commit, idx) => renderCommitRow(commit, idx)).join('') +
                '</div>' +
            '</div>';

            // Attach click handlers to SVG nodes
            container.querySelectorAll('.svg-commit-node').forEach(node => {
                node.addEventListener('click', (e) => {
                    e.stopPropagation();
                    copyShaQuick(node.dataset.shortSha);
                });
            });
        }

        async function selectCommit(sha) {
            selectedCommit = commits.find(c => c.sha === sha);
            if (!selectedCommit) return;

            // Update selection in list
            document.querySelectorAll('.commit-row').forEach(row => {
                row.classList.toggle('selected', row.dataset.sha === sha);
            });

            // Show detail panel
            const panel = document.getElementById('detailPanel');
            panel.classList.remove('hidden');

            document.getElementById('detailSha').textContent = selectedCommit.short_sha;
            document.getElementById('detailMessage').textContent = selectedCommit.subject;
            document.getElementById('detailMeta').innerHTML =
                '<div class="detail-meta-row"><i class="fa-regular fa-user"></i> ' + escapeHtml(selectedCommit.author) + '</div>' +
                '<div class="detail-meta-row"><i class="fa-regular fa-clock"></i> ' + formatTimeFull(selectedCommit.timestamp) + '</div>' +
                '<div class="detail-meta-row"><i class="fa-solid fa-code-branch"></i> ' + selectedCommit.parent_shas.length + ' parent(s)</div>';

            // Load files
            document.getElementById('detailFiles').innerHTML = '<div class="loading"><i class="fa-solid fa-spinner fa-spin"></i></div>';
            try {
                const res = await fetch('/api/checkpoints/' + sha + '/diff', { credentials: 'same-origin' });
                const data = await res.json();
                const files = data.changed_files || [];
                if (files.length === 0) {
                    document.getElementById('detailFiles').innerHTML = '<div style="color:#888; font-style:italic;">No files changed</div>';
                } else {
                    document.getElementById('detailFiles').innerHTML = files.map(f =>
                        '<div class="file-item"><i class="fa-regular fa-file-code"></i><span class="file-name">' + escapeHtml(f) + '</span></div>'
                    ).join('');
                }
            } catch (e) {
                document.getElementById('detailFiles').innerHTML = '<div style="color:#e57373;">Failed to load files</div>';
            }
        }

        function closeDetail() {
            document.getElementById('detailPanel').classList.add('hidden');
            document.querySelectorAll('.commit-row').forEach(row => row.classList.remove('selected'));
            selectedCommit = null;
        }

        function copySha() {
            if (selectedCommit) {
                navigator.clipboard.writeText(selectedCommit.sha);
                showToast('Full SHA copied!');
            }
        }

        function copyShaQuick(sha) {
            navigator.clipboard.writeText(sha);
            showToast('Copied: ' + sha);
        }

        async function viewDiff() {
            if (!selectedCommit) return;
            // Open diff in new window (or could show modal)
            window.open('/api/checkpoints/' + selectedCommit.sha + '/diff', '_blank');
        }

        async function rollbackTo() {
            if (!selectedCommit) return;
            if (!confirm('Rollback to commit ' + selectedCommit.short_sha + '?\\n\\nThis will discard all changes after this commit.')) return;

            try {
                const res = await fetch('/api/checkpoints/' + selectedCommit.sha + '/rollback', {
                    method: 'POST',
                    credentials: 'same-origin'
                });
                if (res.ok) {
                    showToast('Rolled back successfully!');
                    loadData();
                } else {
                    showToast('Rollback failed');
                }
            } catch (e) {
                showToast('Rollback failed: ' + e.message);
            }
        }

        async function pushToRemote() {
            if (!confirm('Push all commits to remote?')) return;
            try {
                const res = await fetch('/api/git/push', { method: 'POST', credentials: 'same-origin' });
                const data = await res.json();
                showToast(data.message || (data.success ? 'Pushed!' : 'Push failed'));
            } catch (e) {
                showToast('Push failed: ' + e.message);
            }
        }

        function refreshData() {
            loadData();
            showToast('Refreshed!');
        }

        function showToast(msg) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 2500);
        }

        function formatTime(iso) {
            const d = new Date(iso);
            const now = new Date();
            const diff = now - d;
            if (diff < 60000) return 'just now';
            if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
            if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
            if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago';
            return d.toLocaleDateString();
        }

        function formatTimeFull(iso) {
            return new Date(iso).toLocaleString();
        }

        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Event listeners
        document.getElementById('searchInput').addEventListener('input', () => {
            clearTimeout(window.searchTimeout);
            window.searchTimeout = setTimeout(renderCommits, 300);
        });
        document.getElementById('branchFilter').addEventListener('change', renderCommits);

        // Initial load
        loadData();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/scheduled-jobs", response_class=HTMLResponse)
async def scheduled_jobs_page(user: User = Depends(engineer_or_admin_required)):
    """Serve the scheduled jobs management page."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot Scheduled Jobs</title>
    <link rel="icon" type="image/png" href="/frontend/leonardo-icon.png">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {
            --bg-color: #0d0d1a;
            --chat-bg: #1a1730;
            --text-color: #e0e0e0;
            --border-color: rgba(139, 92, 246, 0.2);
            --accent-color: #8b5cf6;
        }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }
        .container {
            max-width: 1100px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        .header {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 30px;
        }
        .back-btn {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            text-decoration: none;
            transition: background 0.2s;
        }
        .back-btn:hover { background: rgba(139, 92, 246, 0.15); }
        h1 { font-size: 1.5rem; margin: 0; flex: 1; }
        .card {
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }
        .card-header {
            font-size: 0.85rem;
            color: rgba(255, 255, 255, 0.5);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid var(--border-color); }
        th { color: rgba(255, 255, 255, 0.5); font-weight: 500; font-size: 0.85rem; }
        tr:last-child td { border-bottom: none; }
        .badge {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 500;
            text-transform: uppercase;
        }
        .badge-enabled { background: rgba(34, 197, 94, 0.15); color: #22c55e; }
        .badge-disabled { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
        .badge-completed { background: rgba(34, 197, 94, 0.15); color: #22c55e; }
        .badge-running { background: rgba(139, 92, 246, 0.2); color: #a78bfa; }
        .badge-failed { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
        .badge-timeout { background: rgba(251, 191, 36, 0.15); color: #fbbf24; }
        .badge-pending { background: rgba(255, 255, 255, 0.1); color: rgba(255, 255, 255, 0.5); }
        .btn {
            padding: 6px 12px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            margin-right: 4px;
            background: transparent;
            color: var(--text-color);
            transition: all 0.2s;
        }
        .btn:hover { background: rgba(139, 92, 246, 0.15); }
        .btn-primary {
            background: linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%);
            border-color: #8b5cf6;
            color: white;
        }
        .btn-primary:hover { opacity: 0.9; }
        .btn-danger { border-color: rgba(239, 68, 68, 0.25); color: #ef4444; }
        .btn-danger:hover { background: rgba(239, 68, 68, 0.15); }
        .btn-sm { padding: 4px 8px; font-size: 11px; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; margin-bottom: 6px; font-size: 0.85rem; color: rgba(255, 255, 255, 0.5); }
        .form-group input, .form-group select, .form-group textarea {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            box-sizing: border-box;
        }
        .form-group textarea { min-height: 100px; resize: vertical; }
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
            outline: none;
            border-color: rgba(139, 92, 246, 0.4);
            box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.08);
        }
        .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        .message {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: none;
        }
        .message.success { background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.25); color: #22c55e; display: block; }
        .message.error { background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.25); color: #ef4444; display: block; }
        .actions { white-space: nowrap; }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            width: 90%;
            max-width: 600px;
            max-height: 90vh;
            overflow-y: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .modal-header h2 { margin: 0; font-size: 1.2rem; }
        .close-btn {
            background: none;
            border: none;
            color: var(--text-color);
            font-size: 1.5rem;
            cursor: pointer;
        }
        .output-box {
            background: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 12px;
            font-family: monospace;
            font-size: 12px;
            white-space: pre-wrap;
            max-height: 200px;
            overflow-y: auto;
        }
        .tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 20px;
        }
        .tab {
            padding: 8px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            background: transparent;
            color: var(--text-color);
        }
        .tab.active {
            background: var(--accent-color);
            border-color: var(--accent-color);
        }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .cron-help {
            font-size: 11px;
            color: rgba(255, 255, 255, 0.5);
            margin-top: 8px;
        }
        .cron-presets {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-top: 8px;
        }
        .cron-preset {
            padding: 4px 8px;
            background: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 4px;
            font-size: 11px;
            cursor: pointer;
            color: var(--text-color);
            transition: all 0.2s;
        }
        .cron-preset:hover {
            border-color: var(--accent-color);
            background: rgba(139, 92, 246, 0.1);
        }
        .cron-preset code {
            color: #a78bfa;
            margin-left: 4px;
        }
        .toggle {
            position: relative;
            display: inline-block;
            width: 50px;
            height: 26px;
            cursor: pointer;
        }
        .toggle input { display: none; }
        .toggle-slider {
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #555;
            border-radius: 13px;
            transition: 0.3s;
            border: 2px solid #666;
        }
        .toggle-slider:before {
            content: "";
            position: absolute;
            height: 18px;
            width: 18px;
            left: 2px;
            bottom: 2px;
            background: #aaa;
            border-radius: 50%;
            transition: 0.3s;
            box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }
        .toggle input:checked + .toggle-slider {
            background: var(--accent-color);
            border-color: var(--accent-color);
        }
        .toggle input:checked + .toggle-slider:before {
            transform: translateX(24px);
            background: white;
        }
        .empty-state {
            text-align: center;
            padding: 40px;
            color: rgba(255, 255, 255, 0.5);
        }
        .empty-state i { font-size: 48px; margin-bottom: 16px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/settings" class="back-btn">
                <i class="fa-solid fa-arrow-left"></i>
            </a>
            <h1>Scheduled Jobs</h1>
            <button class="btn btn-primary" onclick="openCreateModal()">
                <i class="fa-solid fa-plus"></i> New Job
            </button>
        </div>

        <div id="message" class="message"></div>

        <div class="tabs">
            <button class="tab active" onclick="showTab('jobs')">Jobs</button>
            <button class="tab" onclick="showTab('runs')">Recent Runs</button>
            <button class="tab" onclick="showTab('logs')">Logs</button>
        </div>

        <div id="jobs-tab" class="tab-content active">
            <div class="card">
                <div class="card-header">Scheduled Jobs</div>
                <div id="jobsContainer">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Agent</th>
                                <th>Schedule</th>
                                <th>Last Run</th>
                                <th>Next Run</th>
                                <th>Enabled</th>
                                <th class="actions">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="jobsTable">
                            <tr><td colspan="7">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="runs-tab" class="tab-content">
            <div class="card">
                <div class="card-header">Recent Runs</div>
                <table>
                    <thead>
                        <tr>
                            <th>Job</th>
                            <th>Status</th>
                            <th>Trigger</th>
                            <th>Started</th>
                            <th>Duration</th>
                            <th>Tokens</th>
                            <th class="actions">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="runsTable">
                        <tr><td colspan="7">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div id="logs-tab" class="tab-content">
            <div class="card">
                <div class="card-header">Cron Invocation Logs</div>
                <p style="font-size: 12px; color: #888; margin-bottom: 16px;">
                    Every cron ping to /api/scheduled-jobs/invoke is logged here, even when no jobs are due.
                </p>
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Status</th>
                            <th>Jobs Checked</th>
                            <th>Jobs Run</th>
                            <th>Duration</th>
                            <th>Error</th>
                        </tr>
                    </thead>
                    <tbody id="logsTable">
                        <tr><td colspan="6">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Create/Edit Job Modal -->
    <div id="jobModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 id="modalTitle">Create Job</h2>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <form id="jobForm">
                <input type="hidden" id="jobId">
                <div class="form-group">
                    <label>Name</label>
                    <input type="text" id="jobName" required placeholder="e.g., Daily Code Review">
                </div>
                <div class="form-group">
                    <label>Description (optional)</label>
                    <input type="text" id="jobDescription" placeholder="What this job does">
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Agent</label>
                        <select id="jobAgent" required>
                            <option value="">Loading agents…</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Model</label>
                        <select id="jobModel">
                            <option value="">Loading models…</option>
                        </select>
                    </div>
                </div>
                <div class="form-group">
                    <label>Prompt / Instructions</label>
                    <textarea id="jobPrompt" required placeholder="What should the agent do when it wakes up?"></textarea>
                </div>
                <div class="form-group">
                    <label>Cron Expression</label>
                    <div class="form-row">
                        <input type="text" id="jobCron" required placeholder="0 8 * * *" style="flex: 2;">
                        <select id="jobTimezone" style="flex: 1;">
                            <option value="UTC">UTC</option>
                            <option value="America/Los_Angeles">Pacific (PT)</option>
                            <option value="America/Denver">Mountain (MT)</option>
                            <option value="America/Chicago">Central (CT)</option>
                            <option value="America/New_York">Eastern (ET)</option>
                            <option value="Africa/Johannesburg">South Africa (SAST)</option>
                        </select>
                    </div>
                    <div class="cron-help">
                        Format: <code>minute hour day-of-month month day-of-week</code>
                    </div>
                    <div class="cron-presets">
                        <button type="button" class="cron-preset" onclick="setCron('* * * * *')">Every minute<code>* * * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('*/5 * * * *')">Every 5 min<code>*/5 * * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('*/30 * * * *')">Every 30 min<code>*/30 * * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 * * * *')">Every hour<code>0 * * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 */4 * * *')">Every 4 hours<code>0 */4 * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 8 * * *')">Daily 8am<code>0 8 * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 9 * * 1')">Weekly Mon 9am<code>0 9 * * 1</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 0 1 * *')">Monthly 1st<code>0 0 1 * *</code></button>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Timeout (seconds)</label>
                        <input type="number" id="jobTimeout" value="300" min="60" max="3600">
                    </div>
                    <div class="form-group">
                        <label>Recursion Limit</label>
                        <input type="number" id="jobRecursion" value="100" min="10" max="1000">
                    </div>
                </div>
                <div style="display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px;">
                    <button type="button" class="btn" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save Job</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Run Details Modal -->
    <div id="runModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Run Details</h2>
                <button class="close-btn" onclick="closeRunModal()">&times;</button>
            </div>
            <div id="runDetails"></div>
        </div>
    </div>

    <script>
        let jobs = [];
        let runs = [];

        const MODEL_LABELS = {
            'gemini-3-flash': 'Gemini 3 Flash',
            'gemini-3-pro': 'Gemini 3 Pro',
            'claude-4.5-haiku': 'Claude 4.5 Haiku',
            'claude-4.5-sonnet': 'Claude 4.5 Sonnet',
            'gpt-5-mini': 'GPT-5 Mini',
            'gpt-5-codex': 'GPT-5 Codex',
            'deepseek-v4-flash': 'DeepSeek V4 Flash',
            'deepseek-v4-flash-vision-exp': 'DeepSeek V4 Flash Vision',
            'deepseek-v4-flash-gmi': 'DeepSeek V4 Flash (GMI)',
            'deepseek-v4-flash-fireworks': 'DeepSeek V4 Flash (Fireworks)',
        };

        // Labels for config-registered models arrive with the model list rather
        // than the static map above. Cached on load so a saved job still renders
        // a real name after a reload.
        let dynamicModelLabels = {};
        function modelLabelFromList(value) {
            return dynamicModelLabels[value] || value;
        }

        function prettyAgentLabel(name) {
            return name.replace(/_/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase());
        }

        async function loadAgents() {
            try {
                const response = await fetch('/available-agents');
                const data = await response.json();
                const sel = document.getElementById('jobAgent');
                const agents = data.agents || [];
                sel.innerHTML = agents.map(a =>
                    `<option value="${a}">${prettyAgentLabel(a)}</option>`
                ).join('');
            } catch (e) {
                console.error('Failed to load agents:', e);
            }
        }

        async function loadModels() {
            try {
                const response = await fetch('/api/available-models');
                const data = await response.json();
                const sel = document.getElementById('jobModel');
                const models = data.models || [];
                models.forEach(m => { if (m.label) dynamicModelLabels[m.value] = m.label; });
                sel.innerHTML = models.map(m => {
                    // m.label is sent for config-registered (OpenRouter) models,
                    // which MODEL_LABELS below cannot know about; the static map
                    // stays authoritative for the hardcoded ones.
                    const label = MODEL_LABELS[m.value] || m.label || m.value;
                    const suffix = m.available ? '' : ' (No API Key)';
                    const disabled = m.available ? '' : 'disabled';
                    const title = m.reason ? ` title="${m.reason}"` : '';
                    return `<option value="${m.value}" ${disabled}${title}>${label}${suffix}</option>`;
                }).join('');
                const firstAvailable = models.find(m => m.available);
                if (firstAvailable) sel.value = firstAvailable.value;
            } catch (e) {
                console.error('Failed to load models:', e);
            }
        }

        // If a saved job's agent/model is no longer in the dropdown (graph removed,
        // API key revoked, model deprecated), inject it as a disabled option so the
        // user still sees what was configured and can pick a replacement.
        function ensureOptionPresent(selectEl, value, labelFallback) {
            if (!value) return;
            const existing = Array.from(selectEl.options).find(o => o.value === value);
            if (existing) {
                selectEl.value = value;
                return;
            }
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = `${labelFallback || value} (unavailable)`;
            opt.disabled = true;
            opt.selected = true;
            selectEl.appendChild(opt);
        }

        async function loadJobs() {
            try {
                const response = await fetch('/api/scheduled-jobs');
                jobs = await response.json();
                renderJobs();
            } catch (e) {
                showMessage('Failed to load jobs: ' + e.message, 'error');
            }
        }

        async function loadRuns() {
            try {
                const response = await fetch('/api/scheduled-jobs/runs/recent?limit=50');
                runs = await response.json();
                renderRuns();
            } catch (e) {
                showMessage('Failed to load runs: ' + e.message, 'error');
            }
        }

        function renderJobs() {
            const tbody = document.getElementById('jobsTable');
            if (jobs.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><i class="fa-solid fa-clock"></i><p>No scheduled jobs yet. Create one to get started!</p></div></td></tr>';
                return;
            }
            tbody.innerHTML = jobs.map(job => `
                <tr>
                    <td><strong>${escapeHtml(job.name)}</strong><br><small style="color:#888">${escapeHtml(job.description || '')}</small></td>
                    <td>${escapeHtml(job.agent_name)}</td>
                    <td><code>${escapeHtml(job.cron_expression)}</code><br><small style="color:#888">${escapeHtml(job.timezone)}</small></td>
                    <td>${job.last_run_at ? formatDate(job.last_run_at) : '-'}</td>
                    <td>${job.next_run_at ? formatDate(job.next_run_at) : '-'}</td>
                    <td>
                        <label class="toggle">
                            <input type="checkbox" ${job.is_enabled ? 'checked' : ''} onchange="toggleJob(${job.id}, this.checked)">
                            <span class="toggle-slider"></span>
                        </label>
                    </td>
                    <td class="actions">
                        <button class="btn btn-sm" onclick="runJob(${job.id})" title="Run Now"><i class="fa-solid fa-play"></i></button>
                        <button class="btn btn-sm" onclick="editJob(${job.id})" title="Edit"><i class="fa-solid fa-pen"></i></button>
                        <button class="btn btn-sm" onclick="viewJobRuns(${job.id})" title="View Runs"><i class="fa-solid fa-history"></i></button>
                    </td>
                </tr>
            `).join('');
        }

        function renderRuns() {
            const tbody = document.getElementById('runsTable');
            if (runs.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><i class="fa-solid fa-history"></i><p>No runs yet</p></div></td></tr>';
                return;
            }
            tbody.innerHTML = runs.map(run => {
                const job = jobs.find(j => j.id === run.job_id);
                return `
                    <tr>
                        <td>${job ? escapeHtml(job.name) : 'Job #' + run.job_id}</td>
                        <td><span class="badge badge-${run.status}">${run.status}</span></td>
                        <td>${run.trigger_type}</td>
                        <td>${run.started_at ? formatTimestamp(run.started_at) : '-'}</td>
                        <td>${run.duration_seconds ? run.duration_seconds.toFixed(1) + 's' : '-'}</td>
                        <td>${run.total_tokens > 0 ? run.total_tokens.toLocaleString() : '-'}</td>
                        <td class="actions">
                            <button class="btn btn-sm" onclick="viewRun(${run.id})" title="View Details"><i class="fa-solid fa-eye"></i></button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        function showTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            const tabIndex = tab === 'jobs' ? 1 : (tab === 'runs' ? 2 : 3);
            document.querySelector(`.tab:nth-child(${tabIndex})`).classList.add('active');
            document.getElementById(tab + '-tab').classList.add('active');
            if (tab === 'runs') loadRuns();
            if (tab === 'logs') loadLogs();
        }

        let invocationLogs = [];

        async function loadLogs() {
            try {
                const response = await fetch('/api/scheduled-jobs/invocations?limit=100');
                invocationLogs = await response.json();
                renderLogs();
            } catch (e) {
                showMessage('Failed to load logs: ' + e.message, 'error');
            }
        }

        function renderLogs() {
            const tbody = document.getElementById('logsTable');
            if (invocationLogs.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state"><i class="fa-solid fa-file-lines"></i><p>No invocation logs yet. The cron will log each ping here.</p></div></td></tr>';
                return;
            }
            tbody.innerHTML = invocationLogs.map(log => `
                <tr>
                    <td>${log.invoked_at ? formatTimestamp(log.invoked_at) : '-'}</td>
                    <td><span class="badge badge-${log.status === 'success' ? 'completed' : (log.status === 'no_jobs_due' ? 'pending' : 'failed')}">${log.status}</span></td>
                    <td>${log.jobs_checked}</td>
                    <td>${log.jobs_executed}</td>
                    <td>${log.duration_ms !== null ? log.duration_ms + 'ms' : '-'}</td>
                    <td style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${log.error_message ? escapeHtml(log.error_message) : ''}">${log.error_message ? escapeHtml(log.error_message.substring(0, 50)) + (log.error_message.length > 50 ? '...' : '') : '-'}</td>
                </tr>
            `).join('');
        }

        function openCreateModal() {
            document.getElementById('modalTitle').textContent = 'Create Job';
            document.getElementById('jobForm').reset();
            document.getElementById('jobId').value = '';
            // Refresh in background — initial load already populated these on page load.
            loadAgents();
            loadModels();
            document.getElementById('jobModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('jobModal').classList.remove('active');
        }

        function closeRunModal() {
            document.getElementById('runModal').classList.remove('active');
        }

        async function editJob(id) {
            const job = jobs.find(j => j.id === id);
            if (!job) return;
            document.getElementById('modalTitle').textContent = 'Edit Job';
            document.getElementById('jobId').value = job.id;
            document.getElementById('jobName').value = job.name;
            document.getElementById('jobDescription').value = job.description || '';
            await Promise.all([loadAgents(), loadModels()]);
            ensureOptionPresent(
                document.getElementById('jobAgent'),
                job.agent_name,
                prettyAgentLabel(job.agent_name || '')
            );
            ensureOptionPresent(
                document.getElementById('jobModel'),
                job.llm_model,
                MODEL_LABELS[job.llm_model] || modelLabelFromList(job.llm_model)
            );
            document.getElementById('jobPrompt').value = job.prompt;
            document.getElementById('jobCron').value = job.cron_expression;
            document.getElementById('jobTimezone').value = job.timezone;
            document.getElementById('jobTimeout').value = job.max_duration_seconds;
            document.getElementById('jobRecursion').value = job.recursion_limit;
            document.getElementById('jobModal').classList.add('active');
        }

        async function toggleJob(id, enabled) {
            try {
                await fetch(`/api/scheduled-jobs/${id}/${enabled ? 'enable' : 'disable'}`, { method: 'POST' });
                loadJobs();
            } catch (e) {
                showMessage('Failed to toggle job: ' + e.message, 'error');
            }
        }

        async function runJob(id) {
            if (!confirm('Run this job now?')) return;
            try {
                showMessage('Running job...', 'success');
                const response = await fetch(`/api/scheduled-jobs/${id}/run`, { method: 'POST' });
                const run = await response.json();
                showMessage(`Job completed with status: ${run.status}`, run.status === 'completed' ? 'success' : 'error');
                loadJobs();
                loadRuns();
            } catch (e) {
                showMessage('Failed to run job: ' + e.message, 'error');
            }
        }

        async function viewJobRuns(id) {
            try {
                const response = await fetch(`/api/scheduled-jobs/${id}/runs`);
                runs = await response.json();
                showTab('runs');
                renderRuns();
            } catch (e) {
                showMessage('Failed to load runs: ' + e.message, 'error');
            }
        }

        async function viewRun(id) {
            try {
                const response = await fetch(`/api/scheduled-jobs/runs/${id}`);
                const run = await response.json();
                const job = jobs.find(j => j.id === run.job_id);
                document.getElementById('runDetails').innerHTML = `
                    <p><strong>Job:</strong> ${job ? escapeHtml(job.name) : 'Job #' + run.job_id}</p>
                    <p><strong>Status:</strong> <span class="badge badge-${run.status}">${run.status}</span></p>
                    <p><strong>Trigger:</strong> ${run.trigger_type}</p>
                    <p><strong>Started:</strong> ${run.started_at ? formatTimestamp(run.started_at) : '-'}</p>
                    <p><strong>Duration:</strong> ${run.duration_seconds ? run.duration_seconds.toFixed(2) + ' seconds' : '-'}</p>
                    <p><strong>Tokens:</strong> ${run.input_tokens} in / ${run.output_tokens} out (${run.total_tokens} total)</p>
                    <p><strong>Thread ID:</strong> <code>${run.thread_id}</code></p>
                    ${run.error_type ? `<p><strong>Error Type:</strong> <code style="color:#e57373">${escapeHtml(run.error_type)}</code></p>` : ''}
                    ${run.error_message ? `<p><strong>Error Message:</strong></p><div class="output-box" style="color:#e57373">${escapeHtml(run.error_message)}</div>` : ''}
                    ${run.error_traceback ? `
                        <details style="margin-top: 12px;">
                            <summary style="cursor: pointer; color: #e57373;"><strong>Stack Trace</strong> (click to expand)</summary>
                            <div class="output-box" style="color:#e57373; margin-top: 8px; max-height: 300px;">${escapeHtml(run.error_traceback)}</div>
                        </details>
                    ` : ''}
                    ${run.output_summary ? `<p><strong>Output:</strong></p><div class="output-box">${escapeHtml(run.output_summary)}</div>` : ''}
                `;
                document.getElementById('runModal').classList.add('active');
            } catch (e) {
                showMessage('Failed to load run details: ' + e.message, 'error');
            }
        }

        document.getElementById('jobForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const id = document.getElementById('jobId').value;
            const data = {
                name: document.getElementById('jobName').value,
                description: document.getElementById('jobDescription').value || null,
                agent_name: document.getElementById('jobAgent').value,
                llm_model: document.getElementById('jobModel').value,
                prompt: document.getElementById('jobPrompt').value,
                cron_expression: document.getElementById('jobCron').value,
                timezone: document.getElementById('jobTimezone').value,
                max_duration_seconds: parseInt(document.getElementById('jobTimeout').value),
                recursion_limit: parseInt(document.getElementById('jobRecursion').value),
            };
            try {
                if (id) {
                    await fetch(`/api/scheduled-jobs/${id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    showMessage('Job updated!', 'success');
                } else {
                    await fetch('/api/scheduled-jobs', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    showMessage('Job created!', 'success');
                }
                closeModal();
                loadJobs();
            } catch (e) {
                showMessage('Failed to save job: ' + e.message, 'error');
            }
        });

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => { msg.className = 'message'; }, 4000);
        }

        function formatDate(isoString) {
            const d = new Date(isoString);
            const now = new Date();
            const diff = now - d;
            if (diff < 60000) return 'just now';
            if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
            if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
            return d.toLocaleDateString();
        }

        function formatTimestamp(isoString) {
            if (!isoString) return '-';
            const d = new Date(isoString);
            return d.toLocaleString('en-US', {
                month: 'short',
                day: 'numeric',
                year: 'numeric',
                hour: 'numeric',
                minute: '2-digit',
                hour12: true,
                timeZoneName: 'short'
            });
        }

        function escapeHtml(text) {
            if (!text) return '';
            return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        function setCron(expression) {
            document.getElementById('jobCron').value = expression;
        }

        // Load data on page load
        loadJobs();
        loadAgents();
        loadModels();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/backup-history", response_class=HTMLResponse)
async def backup_history_page(current_user: User = Depends(get_current_user)):
    """Serve the backup history page."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Backup History</title>
    <link rel="icon" type="image/png" href="/frontend/leonardo-icon.png">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {
            --bg-color: #0d0d1a;
            --chat-bg: #1a1730;
            --text-color: #e0e0e0;
            --border-color: rgba(139, 92, 246, 0.2);
            --accent-color: #8b5cf6;
        }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        .header {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 30px;
        }
        h1 {
            font-size: 1.5rem;
            margin: 0;
        }
        .backup-entry {
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 12px;
        }
        .backup-entry.completed {
            border-left: 3px solid #22c55e;
        }
        .backup-entry.failed {
            border-left: 3px solid #ef4444;
        }
        .backup-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }
        .backup-status-badge {
            font-size: 0.8rem;
            font-weight: 600;
            padding: 2px 8px;
            border-radius: 4px;
        }
        .backup-status-badge.completed {
            background: rgba(34, 197, 94, 0.15);
            color: #22c55e;
        }
        .backup-status-badge.failed {
            background: rgba(239, 68, 68, 0.15);
            color: #ef4444;
        }
        .backup-time {
            font-size: 0.85rem;
            color: rgba(255, 255, 255, 0.5);
        }
        .backup-error {
            font-size: 0.8rem;
            color: #ef4444;
            background: rgba(239, 68, 68, 0.1);
            padding: 8px 12px;
            border-radius: 4px;
            margin-top: 8px;
            font-family: monospace;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .backup-output {
            font-size: 0.75rem;
            color: rgba(255, 255, 255, 0.6);
            background: #0d0d1a;
            padding: 8px 12px;
            border-radius: 4px;
            margin-top: 8px;
            font-family: monospace;
            white-space: pre-wrap;
            max-height: 200px;
            overflow-y: auto;
            word-break: break-all;
        }
        .backup-output summary {
            cursor: pointer;
            color: rgba(255, 255, 255, 0.5);
            font-size: 0.75rem;
            margin-bottom: 4px;
        }
        .empty-state {
            text-align: center;
            color: rgba(255, 255, 255, 0.5);
            padding: 60px 20px;
        }
        .empty-state i {
            font-size: 3rem;
            margin-bottom: 16px;
            display: block;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1><i class="fa-solid fa-cloud-arrow-up" style="margin-right: 10px;"></i>Backup History</h1>
        </div>
        <div id="backup-list">
            <div class="empty-state">
                <i class="fa-solid fa-spinner fa-spin"></i>
                Loading...
            </div>
        </div>
    </div>

    <script>
        async function loadHistory() {
            try {
                const res = await fetch('/api/auto-backup/history');
                const history = await res.json();
                const container = document.getElementById('backup-list');

                if (!history || history.length === 0) {
                    container.innerHTML = `
                        <div class="empty-state">
                            <i class="fa-solid fa-box-open"></i>
                            <p>No backup history yet</p>
                        </div>
                    `;
                    return;
                }

                container.innerHTML = history.map(entry => {
                    const date = new Date(entry.timestamp);
                    const timeStr = date.toLocaleString('en-US', {
                        timeZone: 'America/Los_Angeles',
                        year: 'numeric', month: 'short', day: 'numeric',
                        hour: 'numeric', minute: '2-digit', second: '2-digit',
                        hour12: true, timeZoneName: 'short'
                    });
                    const errorHtml = entry.error
                        ? `<div class="backup-error">${escapeHtml(entry.error)}</div>`
                        : '';
                    const outputHtml = entry.stdout
                        ? `<details class="backup-output"><summary>Show output</summary>${escapeHtml(entry.stdout)}</details>`
                        : '';

                    return `
                        <div class="backup-entry ${entry.status}">
                            <div class="backup-header">
                                <span class="backup-time">${timeStr}</span>
                                <span class="backup-status-badge ${entry.status}">${entry.status === 'completed' ? 'Success' : 'Failed'}</span>
                            </div>
                            ${errorHtml}
                            ${outputHtml}
                        </div>
                    `;
                }).join('');
            } catch (e) {
                document.getElementById('backup-list').innerHTML = `
                    <div class="empty-state">
                        <i class="fa-solid fa-triangle-exclamation"></i>
                        <p>Failed to load backup history</p>
                    </div>
                `;
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        loadHistory();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)
