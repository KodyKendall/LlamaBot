"""Role → agent-mode permissions. The single source of truth.

One question — *may this role use this agent mode?* — asked in exactly two places:

  * ``app/routers/ui.py``               builds the mode dropdown for the browser
  * ``app/websocket/web_socket_handler.py``  rejects a frame naming a mode the
    user was never granted

Both call :func:`allowed_modes`. The dropdown is a *view* of the permission set,
never a second copy of the rule — that's what kept them from drifting before
(the dropdown filtered client-side while the socket accepted any ``agent_name``).

Storage is one ``SiteSetting`` row (``role_agent_modes``) holding JSON::

    {"user": ["feedback"], "engineer": ["ticket", "engineer", ...]}

Absent, unparseable, or DB-unavailable → :data:`DEFAULT_ROLE_MODES`. A role the
admin has never configured keeps its default; once configured, that list is
authoritative. Admin edits it via ``PUT /api/role-modes``.

Design rules, chosen deliberately:
  * ``is_admin`` is a **superset** — always every mode. An admin editing the role
    table can't lock themselves out of the UI they're editing with.
  * ``User.visible_agents`` **narrows only**. It's a per-user view preference and
    can never widen past the role's grant, so "what can this user do?" is always
    one lookup: the role.
  * An unknown/missing role gets ``user``'s modes — least privilege. (The column
    has a server_default, so real rows always carry a value; this is for safety,
    and it settles the old getattr(role, 'user') vs getattr(role, 'engineer')
    disagreement between dependencies.py and ui.py in favour of the safe answer.)
"""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

ROLE_MODES_SETTING_KEY = "role_agent_modes"

# Built-in agent-mode dropdown keys baked into the image (chat.html <option>s +
# config.js agentModes). Per-instance custom modes may NOT shadow these.
# Superset of MODE_AGENTS: `plan` is an execution toggle, not a grantable mode,
# but a custom mode still must not claim the key.
BUILTIN_AGENT_MODE_KEYS = frozenset({
    "engineer", "ai_builder", "testing", "ticket", "database",
    "beginner", "pyxl", "plan", "chat",
})

# Mode key → EVERY graph that mode can route to.
#
# This map is the crux. The browser picks a mode key, but the wire carries an
# `agent_name` (a langgraph graph name), and the translation lives in the
# frontend — so the server cannot gate on the mode key it never receives. It
# resolves the agent_name back through this map instead.
#
# One mode reaches two graphs because the Plan toggle swaps the agent:
#   base agent   — app/frontend/chat/config.js  (DEFAULT_CONFIG.agentModes)
#   plan variant — app/frontend/chat/index.js   (planAgentByMode, else the
#                  generic rails_plan_mode_agent)
#
# Duplicating the frontend's map here is a drift risk, so it's pinned:
# test_permissions.py::test_mode_agents_matches_frontend parses both JS files
# and fails if they diverge. Change one, change the other.
_GENERIC_PLAN_AGENT = "rails_plan_mode_agent"

MODE_AGENTS: dict[str, frozenset[str]] = {
    "engineer":   frozenset({"rails_agent", "rails_engineer_plan_mode_agent"}),
    "ticket":     frozenset({"rails_ticket_mode_agent", "rails_ticket_plan_mode_agent"}),
    "database":   frozenset({"rails_user_mode_agent", _GENERIC_PLAN_AGENT}),
    "ai_builder": frozenset({"rails_ai_builder_agent", _GENERIC_PLAN_AGENT}),
    "testing":    frozenset({"rails_testing_agent", _GENERIC_PLAN_AGENT}),
    "beginner":   frozenset({"rails_beginner_agent", _GENERIC_PLAN_AGENT}),
    "pyxl":       frozenset({"pyxl_agent", _GENERIC_PLAN_AGENT}),
    # Plain LLM, no tools. Its capability ceiling is the `user` role's ceiling —
    # see app/agents/leonardo/rails_plain_chat_mode/nodes.py. No plan variant:
    # there's nothing to plan without tools.
    "chat":       frozenset({"rails_plain_chat_mode"}),
    # NOTE: user-scoped Rails API access is deliberately NOT a built-in mode.
    # LlamaBot ships only the library (app/lib/llamapress_api.py); the working
    # agent lives downstream (Leonardo langgraph/agents/leo, registered via the
    # langgraph.local.json overlay + agent_modes.json).
}

# The fallback grant per role, used until an admin configures that role.
# Order matters: it drives the dropdown order.
DEFAULT_ROLE_MODES: dict[str, list[str]] = {
    # Plain LLM only. `chat` has no tools by construction, so this role can't
    # touch the user's app at all — that's the ceiling, enforced by the graph
    # itself rather than by trusting the mode not to misbehave.
    "user": ["chat"],
    "engineer": [
        "ticket", "engineer", "testing", "database",
        "ai_builder", "beginner", "pyxl", "chat",
    ],
    # The llama_bot_rails gem — the chat embedded in the box's own Rails app.
    # A verified gem token proves the box's Rails app sent the frame, but it
    # carries a *Rails* user id, which maps to no LlamaBot account and so has no
    # per-user role to look up. The surface gets one grant instead, matching
    # `engineer` because that is what the embedded chat has always been able to
    # do — the point of naming it separately is that an operator can now narrow
    # it, and that the gate checks it at all (it used to skip `rails_auth`).
    "rails": [
        "ticket", "engineer", "testing", "database",
        "ai_builder", "beginner", "pyxl", "chat",
    ],
}

# Role used when a user's role is missing or unrecognised (least privilege).
FALLBACK_ROLE = "user"

# ---------------------------------------------------------------------------
# Tool grants on ZDR boxes (LP-33, HECVAT AILM-01). Unused on a normal box.
#
# An allowlist: a tool added later, or one from a client's own agent, is refused
# until someone decides otherwise. Adding a name here makes it available to every
# role on every institutional box, so it must not change anything.
# ---------------------------------------------------------------------------
READ_ONLY_TOOLS = frozenset({
    # Reading the app
    "ls", "read_file", "grep_files", "glob_files", "git_status", "tail_rails_logs",
    "read_uploaded_file", "view_image", "get_browser_js_logs", "read_leonardo_md", "read_tech_spec",
    "read_brand_guide", "read_skill", "use_skill", "list_skills", "list_memories", "list_goals",
    "ls_agents", "list_books", "get_book",
    # Reading spreadsheets
    "list_spreadsheets", "list_sheets", "read_headers", "get_sheet_dimensions",
    "get_cell_value", "get_chart_data", "sample_rows", "summarize_column",
    "detect_column_types", "data_quality_check", "statistical_analysis",
    "find_patterns_and_anomalies", "find_cross_sheet_relationships", "check_formulas",
    # Talking to the user, and the agent's own notes
    "ask_user_question", "ask_user_uiux_question", "write_todos", "suggest_plan_mode",
    "offer_implementation",
    # Its sub-agent only has read tools, and they are gated here too
    "delegate_research",
    # Under ZDR it only answers "web search is disabled" and never calls the vendor
    "internet_search",
})

ROLE_WRITE_TOOLS_SETTING_KEY = "role_write_tools"
_GRANTS_TTL_SECONDS = 30
_grants_cache: Optional[tuple] = None  # (expires_at, grants)

# Scheduled jobs run with no user. They get this pseudo-role, off until granted,
# in the same way the gem's embedded chat gets ``rails``.
SCHEDULED_ROLE = "scheduled"


def known_modes(custom_modes: Optional[dict] = None) -> set[str]:
    """Every GRANTABLE mode key on this instance (built-in ∪ per-instance custom).

    Grantable means "appears in MODE_AGENTS", not "in BUILTIN_AGENT_MODE_KEYS" —
    the latter also holds `plan`, which is an execution toggle rather than a
    dropdown mode and maps to no graph of its own.
    """
    return set(MODE_AGENTS) | set(custom_modes or {})


def custom_modes() -> dict[str, str]:
    """Per-instance custom modes as {mode_key: agent_name}, validated against the registry.

    For callers that don't already have the overlay loaded (the WebSocket gate).
    ui.py passes its own dict instead — it loads the full mode objects anyway.
    """
    # Deferred: ui.py imports this module at module level, so importing it back
    # at the top would be a cycle.
    from app.routers.ui import load_custom_agent_modes
    try:
        from app.lib.langgraph_registry import load_graphs
        return {
            m["key"]: m["agent_name"]
            for m in load_custom_agent_modes("agent_modes.json", load_graphs())
        }
    except Exception as e:
        logger.warning(f"Could not load custom agent modes: {e}")
        return {}


def _load_configured(session) -> dict[str, list[str]]:
    """Just what the admin explicitly configured. {} when unset/malformed/no DB.

    Kept separate from the defaults because "has this role been configured?" is
    load-bearing: it decides whether custom modes ride along (see allowed_modes).
    """
    from app.models import SiteSetting
    try:
        setting = session.get(SiteSetting, ROLE_MODES_SETTING_KEY)
    except Exception as e:
        # Matches db.py's degradation: no auth DB → defaults, don't hard-fail login.
        logger.warning(f"Could not read '{ROLE_MODES_SETTING_KEY}', using defaults: {e}")
        return {}
    if not setting or not setting.value:
        return {}

    try:
        parsed = json.loads(setting.value)
    except json.JSONDecodeError as e:
        logger.warning(f"Ignoring malformed '{ROLE_MODES_SETTING_KEY}' ({e}); using defaults")
        return {}
    if not isinstance(parsed, dict):
        logger.warning(f"Ignoring '{ROLE_MODES_SETTING_KEY}': expected a JSON object")
        return {}

    return {
        role: [m for m in modes if isinstance(m, str)]
        for role, modes in parsed.items()
        if isinstance(role, str) and isinstance(modes, list)
    }


def get_role_modes(session) -> dict[str, list[str]]:
    """The effective role → modes map: admin config merged over the defaults.

    Only roles the admin has explicitly configured override their default, so
    adding a new role to DEFAULT_ROLE_MODES in code doesn't require a DB edit.
    """
    merged = {role: list(modes) for role, modes in DEFAULT_ROLE_MODES.items()}
    merged.update(_load_configured(session))
    return merged


def allowed_modes(
    session,
    role: Optional[str],
    is_admin: bool = False,
    custom: Optional[dict] = None,
) -> list[str]:
    """Modes this (role, is_admin) may use. Ordered — the dropdown renders it as-is.

    Takes primitives rather than a User because the WebSocket only has JWT
    claims (``role``/``is_admin``), never a User row. See
    :func:`app.services.token_service.create_ws_token`.
    """
    custom = custom or {}

    if is_admin:
        # Superset. Ordered: the engineer default first (canonical dropdown
        # order), then anything else that exists, then custom.
        ordered = list(DEFAULT_ROLE_MODES["engineer"])
        ordered += [m for m in sorted(MODE_AGENTS) if m not in ordered]
        return ordered + [m for m in custom if m not in ordered]

    configured = _load_configured(session)
    effective = {**DEFAULT_ROLE_MODES, **configured}
    if role not in effective:
        role = FALLBACK_ROLE

    modes = list(effective.get(role, DEFAULT_ROLE_MODES[FALLBACK_ROLE]))

    # Custom modes ride along with a role's *default* grant only (preserves the
    # pre-existing "engineer sees new custom modes without an admin edit"
    # behaviour). Once an admin configures the role explicitly, their list wins
    # and custom keys must be opted in there — no surprise grants.
    if role not in configured and role != FALLBACK_ROLE:
        modes += [m for m in custom if m not in modes]

    # Never grant a key that doesn't exist on this instance (stale config).
    valid = known_modes(custom)
    return [m for m in modes if m in valid]


def allowed_agent_names(
    session,
    role: Optional[str],
    is_admin: bool = False,
    custom: Optional[dict] = None,
) -> set[str]:
    """Every graph this (role, is_admin) may run — the set the WebSocket gates on.

    The wire carries an ``agent_name`` (a graph), never the mode key, so the grant
    has to be expanded through MODE_AGENTS before it can be checked. One mode
    yields up to two graphs (base + plan variant).
    """
    custom = custom or {}
    names: set[str] = set()
    for mode in allowed_modes(session, role, is_admin, custom):
        names |= MODE_AGENTS.get(mode, frozenset())
        if mode in custom:
            names.add(custom[mode])
    return names


def visible_modes(
    session,
    user,
    custom: Optional[dict] = None,
) -> list[str]:
    """Modes to show this user in the dropdown = their grant, narrowed by preference.

    ``user.visible_agents`` (JSON array) can only *hide* granted modes; a key it
    names that the role doesn't grant is dropped. A malformed or empty value
    means "no preference" → the full grant.
    """
    granted = allowed_modes(
        session,
        getattr(user, "role", None),
        getattr(user, "is_admin", False),
        custom,
    )

    raw = getattr(user, "visible_agents", None)
    if not raw:
        return granted
    try:
        preferred = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return granted
    if not isinstance(preferred, list) or not preferred:
        return granted

    narrowed = [m for m in granted if m in preferred]
    # An empty intersection means the preference is stale (e.g. the role lost
    # those modes). Fall back to the grant rather than a dead dropdown.
    return narrowed or granted


def can_run_agent(
    session,
    role: Optional[str],
    is_admin: bool,
    agent_name: str,
    custom: Optional[dict] = None,
) -> bool:
    """Authorization check for one ``agent_name`` off the wire. The WebSocket's gate.

    Takes the graph name, not the mode key, because that's what the client sends
    — gating on the mode key would compare two different namespaces and deny
    everyone.
    """
    return agent_name in allowed_agent_names(session, role, is_admin, custom)


def role_write_tool_grants() -> dict[str, bool]:
    """Which roles an admin has given write and shell tools. Off unless saved."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import SiteSetting

    with Session(engine) as session:
        setting = session.get(SiteSetting, ROLE_WRITE_TOOLS_SETTING_KEY)
        grants = json.loads(setting.value) if setting and setting.value else {}
    return {role: value is True for role, value in grants.items()} if isinstance(grants, dict) else {}


def _cached_grants() -> dict[str, bool]:
    """Checked on every tool call, so cached briefly. Failing to read grants nothing."""
    global _grants_cache
    now = time.monotonic()
    if _grants_cache and _grants_cache[0] > now:
        return _grants_cache[1]
    try:
        grants = role_write_tool_grants()
    except Exception as e:  # noqa: BLE001
        logger.error("Could not read role tool grants, granting none: %s", e)
        return {}
    _grants_cache = (now + _GRANTS_TTL_SECONDS, grants)
    return grants


def invalidate_tool_grants() -> None:
    global _grants_cache
    _grants_cache = None


def tool_allowed(tool_name: str, role: Optional[str], is_admin: bool = False) -> bool:
    """On a ZDR box, may this role run this tool? Admins always may."""
    if is_admin or tool_name in READ_ONLY_TOOLS:
        return True
    if not role:
        return False
    return _cached_grants().get(role, False) is True


# ---------------------------------------------------------------------------
# Separation of duties (LP-32, HECVAT APPL-05)
#
#   owner (is_admin)     both of the below, and the only one who may grant both
#   security admin       users, role grants, security policy, audit log
#   system admin         settings, deploys, agent modes, data
#   standard             everyone else
#
# Rails has the same three roles on its own users table (llama_bot_rails
# AdministrativeRoles). The two sides are separate accounts: same rule, not one record.
# ---------------------------------------------------------------------------

# Routes that change the system. On a normal box an engineer keeps them; a security
# admin never has them, and under ZDR only a system admin does.
SYSTEM_ROUTES = (
    "/api/update", "/api/env-vars", "/api/env-toggles", "/api/custom-env-vars",
    "/api/site-settings", "/api/vscode", "/api/skills", "/api/leonardo-md",
    "/api/write_files", "/api/setup", "/api/slash-commands", "/api/github",
    "/api/auto-backup", "/api/scheduled-jobs", "/api/checkpoints", "/api/git",
    "/api/instance-lock", "/api/role-modes",
)
# The ones among those that the chat UI also reads, so only writing them is refused.
_SYSTEM_READS_ALLOWED = (
    "/api/site-settings", "/api/skills", "/api/slash-commands", "/api/checkpoints",
    "/api/git", "/api/instance-lock", "/api/leonardo-md", "/api/update", "/api/github/status",
)
SYSTEM_PAGES = ("/settings/environment", "/scheduled-jobs", "/git-history")

# A security-only admin may change nothing else. Default-deny, so a route added later is
# closed to them until someone decides otherwise.
SECURITY_ADMIN_WRITES = (
    "/api/users", "/api/role-write-tools", "/api/sensitive-data-rules",
    "/api/user/visible-agents", "/api/update-activity", "/api/feedback",
    "/api/frontend-error", "/logout",
)

_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _matches(path: str, routes: tuple) -> bool:
    return any(path == route or path.startswith(route + "/") for route in routes)


def holds_security_admin(user) -> bool:
    return getattr(user, "is_admin", False) is True or getattr(user, "is_security_admin", False) is True


def holds_system_admin(user) -> bool:
    return getattr(user, "is_admin", False) is True or getattr(user, "is_system_admin", False) is True


def is_security_only(user) -> bool:
    return holds_security_admin(user) and not holds_system_admin(user)


def holds_admin_role(user) -> bool:
    return holds_security_admin(user) or holds_system_admin(user)


def route_allowed(user, method: str, path: str, zdr_enforced: bool) -> bool:
    """The domain boundary, checked on every signed-in request."""
    if getattr(user, "is_admin", False) is True:
        return True
    security_only = is_security_only(user)
    write = method.upper() not in _READ_METHODS
    system_route = _matches(path, SYSTEM_ROUTES) and (write or not _matches(path, _SYSTEM_READS_ALLOWED))
    if system_route or _matches(path, SYSTEM_PAGES):
        return holds_system_admin(user) or (not security_only and not zdr_enforced)
    if security_only and write:
        return _matches(path, SECURITY_ADMIN_WRITES)
    return True


def admin_change_refusal(actor, target, changes: dict, reason: Optional[str]) -> Optional[tuple]:
    """Why ``actor`` may not make ``changes`` to ``target``: ``(status, message)``, or None.

    ``target`` is None when the account is being created.
    """
    role_keys = {"is_admin", "is_security_admin", "is_system_admin"}
    touches_roles = any(key in changes for key in role_keys)
    actor_is_owner = bool(actor.is_admin)

    if not holds_security_admin(actor):
        return 403, "Security administrator required"
    if changes.get("is_admin") is not None and not actor_is_owner:
        return 403, "Only an account owner can change owner status"
    if target is not None and target.is_admin and not actor_is_owner:
        return 403, "Only an account owner can change another owner"
    if target is not None and target.id == actor.id and touches_roles and not actor_is_owner:
        return 403, "You cannot change your own roles"
    if (
        target is not None
        and changes.get("new_password") is not None
        and holds_admin_role(target)
        and not actor_is_owner
    ):
        # Setting it would hand over everything that administrator can do.
        return 403, "Only an account owner can set an administrator's password"

    security = changes.get("is_security_admin", getattr(target, "is_security_admin", False))
    system = changes.get("is_system_admin", getattr(target, "is_system_admin", False))
    if touches_roles and security and system:
        if not actor_is_owner:
            return 403, "Only an account owner can give one person both administrator roles"
        if not (reason or "").strip():
            return 400, "Giving one person both administrator roles needs a logged reason"
    return None
