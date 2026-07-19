"""Role → agent-mode permissions (app/permissions.py).

The rule these lock down: the mode dropdown and the WebSocket gate are two views
of ONE grant. Before this, the dropdown filtered client-side and the socket took
any `agent_name` off the wire — so a `user`-role account could run any agent by
editing one field in a frame. test_websocket_mode_enforcement.py covers the gate;
this covers the resolver they both call.
"""
import json
import re
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.permissions import (
    DEFAULT_ROLE_MODES,
    MODE_AGENTS,
    ROLE_MODES_SETTING_KEY,
    allowed_agent_names,
    allowed_modes,
    can_run_agent,
    get_role_modes,
    visible_modes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "app" / "frontend" / "chat"


@pytest.fixture
def session():
    """In-memory DB with the real SiteSetting table (no mocks — this is a schema test too)."""
    import app.models  # noqa: F401  (registers the tables on SQLModel.metadata)

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _configure(session, mapping):
    """Write the admin's role→modes config, as PUT /api/role-modes does."""
    from app.models import SiteSetting

    session.add(SiteSetting(key=ROLE_MODES_SETTING_KEY, value=json.dumps(mapping)))
    session.commit()


class _User:
    """Stand-in for the User row — the resolver only ever reads these three."""

    def __init__(self, role=None, is_admin=False, visible_agents=None):
        self.role = role
        self.is_admin = is_admin
        self.visible_agents = visible_agents


# --- defaults / unconfigured instance -------------------------------------

def test_unconfigured_roles_get_their_defaults(session):
    assert allowed_modes(session, "user") == DEFAULT_ROLE_MODES["user"]
    assert allowed_modes(session, "engineer") == DEFAULT_ROLE_MODES["engineer"]


def test_user_role_cannot_use_engineer_modes_by_default(session):
    assert can_run_agent(session, "user", False, "rails_plain_chat_mode") is True
    assert can_run_agent(session, "user", False, "rails_agent") is False
    assert can_run_agent(session, "user", False, "pyxl_agent") is False


# --- the namespace the wire actually speaks -------------------------------
# The client sends `agent_name` (a graph), never the mode key. Gating on mode
# keys compared two different namespaces and denied everyone, admins included —
# caught only by driving a real socket, so it's pinned here.

def test_grant_is_checked_in_graph_space_not_mode_space(session):
    """`engineer` is a mode key; `rails_agent` is what actually arrives."""
    assert "engineer" in allowed_modes(session, "engineer")
    assert can_run_agent(session, "engineer", False, "rails_agent") is True
    # The mode key itself is NOT a graph and must never authorize a run.
    assert can_run_agent(session, "engineer", False, "engineer") is False


def test_admin_can_run_the_real_graph_names(session):
    """The regression that locked admins out."""
    for graph in ("rails_agent", "rails_beginner_agent", "pyxl_agent", "rails_plain_chat_mode"):
        assert can_run_agent(session, "engineer", True, graph) is True


def test_a_mode_grants_its_plan_variant_too(session):
    """Toggling Plan swaps the graph; it must not trip the gate."""
    assert can_run_agent(session, "engineer", False, "rails_engineer_plan_mode_agent") is True
    assert can_run_agent(session, "engineer", False, "rails_plan_mode_agent") is True  # via beginner etc.


def test_chat_mode_grants_no_tool_using_plan_agent(session):
    """The escape hatch this closes: chat + Plan must not reach the generic plan agent.

    rails_plan_mode_agent HAS tools. If `chat` granted it, the `user` role — whose
    whole point is a no-tools ceiling — could toggle Plan and escape.
    """
    assert allowed_modes(session, "user") == ["chat"]
    assert can_run_agent(session, "user", False, "rails_plain_chat_mode") is True
    assert can_run_agent(session, "user", False, "rails_plan_mode_agent") is False


def test_unknown_graph_is_denied(session):
    assert can_run_agent(session, "engineer", False, "llamabot") is False
    assert can_run_agent(session, "engineer", False, "totally_made_up") is False


# --- parity with the frontend ---------------------------------------------
# MODE_AGENTS duplicates a map that lives in JS. These parse the JS so the two
# cannot drift silently.

def _parse_agent_modes_js():
    """Pull DEFAULT_CONFIG.agentModes out of config.js as {mode: agent_name}."""
    src = (FRONTEND / "config.js").read_text()
    block = re.search(r"agentModes:\s*\{(.*?)\}", src, re.S)
    assert block, "could not find agentModes in config.js"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


def _parse_plan_agents_js():
    """Pull planAgentByMode out of index.js as {mode: plan_agent_name}."""
    src = (FRONTEND / "index.js").read_text()
    block = re.search(r"planAgentByMode\s*=\s*\{(.*?)\}", src, re.S)
    assert block, "could not find planAgentByMode in index.js"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


def test_mode_agents_covers_every_frontend_base_agent():
    """Every mode the dropdown can select must be grantable server-side."""
    js = _parse_agent_modes_js()
    # These are agent_name aliases, not selectable dropdown modes.
    aliases = {"plan", "engineer_plan", "ticket_plan"}
    for mode, agent_name in js.items():
        if mode in aliases:
            continue
        assert mode in MODE_AGENTS, f"config.js has mode '{mode}' with no MODE_AGENTS entry"
        assert agent_name in MODE_AGENTS[mode], (
            f"config.js maps {mode} -> {agent_name}, missing from MODE_AGENTS[{mode}]"
        )


def test_mode_agents_has_no_modes_the_frontend_cannot_select():
    """A grantable mode with no dropdown entry is dead config (this is how `feedback` rotted)."""
    js = _parse_agent_modes_js()
    for mode in MODE_AGENTS:
        assert mode in js, f"MODE_AGENTS has '{mode}' but config.js cannot select it"


def test_mode_agents_covers_every_frontend_plan_variant():
    for mode, plan_agent in _parse_plan_agents_js().items():
        assert plan_agent in MODE_AGENTS[mode], (
            f"index.js maps {mode} + Plan -> {plan_agent}, missing from MODE_AGENTS[{mode}]"
        )


def test_every_builtin_mode_resolves_to_a_registered_graph():
    """A grantable mode pointing at an unregistered graph is a dead dropdown entry.

    Reads app/langgraph.json directly rather than load_graphs(), which resolves
    against the CWD. NOTE: the repo ALSO has a langgraph.json at its root with a
    stale subset of graphs — the app never reads it (uvicorn's CWD is app/, so
    app/langgraph.json shadows it). This asserts against the one that's live.
    """
    base = json.loads((REPO_ROOT / "app" / "langgraph.json").read_text())
    registered = set(base["graphs"])

    granted = {g for graphs in MODE_AGENTS.values() for g in graphs}
    missing = sorted(granted - registered)
    assert not missing, f"grantable modes map to unregistered graphs: {missing}"


# --- admin-configured grants ----------------------------------------------

def test_admin_config_overrides_the_default(session):
    _configure(session, {"user": ["chat", "beginner"]})
    assert allowed_modes(session, "user") == ["chat", "beginner"]
    assert can_run_agent(session, "user", False, "rails_beginner_agent") is True


def test_configuring_one_role_leaves_others_on_defaults(session):
    _configure(session, {"user": ["beginner"]})
    assert allowed_modes(session, "engineer") == DEFAULT_ROLE_MODES["engineer"]


def test_config_can_revoke_a_mode_from_engineer(session):
    _configure(session, {"engineer": ["ticket", "engineer"]})
    assert can_run_agent(session, "engineer", False, "pyxl_agent") is False
    assert can_run_agent(session, "engineer", False, "rails_ticket_mode_agent") is True


def test_config_order_drives_dropdown_order(session):
    _configure(session, {"user": ["pyxl", "chat", "beginner"]})
    assert allowed_modes(session, "user") == ["pyxl", "chat", "beginner"]


# --- admin is a superset ---------------------------------------------------

def test_admin_gets_every_mode_regardless_of_role(session):
    _configure(session, {"user": []})
    granted = allowed_modes(session, "user", is_admin=True)
    assert set(granted) == set(MODE_AGENTS)  # `plan` is a toggle, not a grantable mode


def test_admin_cannot_lock_themselves_out(session):
    """The point of the superset rule: an admin editing the role table keeps access."""
    _configure(session, {"user": [], "engineer": []})
    assert allowed_modes(session, "engineer", is_admin=True) != []


def test_admin_gets_custom_modes_too(session):
    granted = allowed_modes(session, "user", is_admin=True, custom={"my_agent": "some_graph"})
    assert "my_agent" in granted


# --- least privilege on unknown/missing role -------------------------------

def test_unknown_role_falls_back_to_user_not_engineer(session):
    """Settles the old dependencies.py('user') vs ui.py('engineer') split — safe answer wins."""
    assert allowed_modes(session, "wat") == DEFAULT_ROLE_MODES["user"]
    assert allowed_modes(session, None) == DEFAULT_ROLE_MODES["user"]
    assert can_run_agent(session, None, False, "rails_agent") is False


# --- malformed config degrades to defaults, never to open ------------------

@pytest.mark.parametrize("bad", ["not json", "[]", '"a string"', "null", ""])
def test_malformed_config_falls_back_to_defaults(session, bad):
    from app.models import SiteSetting

    session.add(SiteSetting(key=ROLE_MODES_SETTING_KEY, value=bad))
    session.commit()
    assert allowed_modes(session, "user") == DEFAULT_ROLE_MODES["user"]
    assert can_run_agent(session, "user", False, "rails_agent") is False


def test_junk_entries_within_valid_json_are_skipped(session):
    _configure(session, {"user": ["chat", 42, None], "bogus": "not-a-list"})
    assert allowed_modes(session, "user") == ["chat"]


def test_unknown_mode_keys_in_config_are_dropped(session):
    """A stale config naming a mode that no longer exists must not grant it."""
    _configure(session, {"user": ["chat", "deleted_mode"]})
    assert allowed_modes(session, "user") == ["chat"]


def test_db_unavailable_falls_back_to_defaults(session):
    class _Broken:
        def get(self, *a, **kw):
            raise RuntimeError("no auth DB")

    assert allowed_modes(_Broken(), "user") == DEFAULT_ROLE_MODES["user"]
    assert can_run_agent(_Broken(), "user", False, "rails_agent") is False


# --- custom modes ----------------------------------------------------------

def test_custom_modes_ride_along_with_an_unconfigured_engineer_default(session):
    granted = allowed_modes(session, "engineer", custom={"my_agent": "some_graph"})
    assert "my_agent" in granted


def test_custom_modes_do_not_ride_along_for_the_user_role(session):
    granted = allowed_modes(session, "user", custom={"my_agent": "some_graph"})
    assert "my_agent" not in granted


def test_configured_role_must_opt_into_custom_modes_explicitly(session):
    """Once an admin sets a role's list, nothing gets auto-added behind their back."""
    _configure(session, {"engineer": ["ticket"]})
    assert allowed_modes(session, "engineer", custom={"my_agent": "some_graph"}) == ["ticket"]

    _configure_again = {"engineer": ["ticket", "my_agent"]}
    from app.models import SiteSetting

    session.get(SiteSetting, ROLE_MODES_SETTING_KEY).value = json.dumps(_configure_again)
    session.commit()
    assert "my_agent" in allowed_modes(session, "engineer", custom={"my_agent": "some_graph"})


# --- visible_agents narrows, never widens ----------------------------------

def test_visible_agents_narrows_the_grant(session):
    user = _User(role="engineer", visible_agents=json.dumps(["ticket", "engineer"]))
    assert visible_modes(session, user) == ["ticket", "engineer"]


def test_visible_agents_cannot_widen_past_the_role(session):
    """The escalation this closes: a per-user list naming a mode the role lacks."""
    user = _User(role="user", visible_agents=json.dumps(["chat", "pyxl"]))
    assert visible_modes(session, user) == ["chat"]


def test_visible_agents_unset_means_the_full_grant(session):
    user = _User(role="engineer", visible_agents=None)
    assert visible_modes(session, user) == DEFAULT_ROLE_MODES["engineer"]


@pytest.mark.parametrize("raw", ["", "not json", "{}", "[]"])
def test_malformed_visible_agents_means_no_preference(session, raw):
    user = _User(role="engineer", visible_agents=raw)
    assert visible_modes(session, user) == DEFAULT_ROLE_MODES["engineer"]


def test_stale_visible_agents_does_not_produce_an_empty_dropdown(session):
    """Preference names only revoked modes → fall back to the grant, not a dead UI."""
    _configure(session, {"engineer": ["ticket"]})
    user = _User(role="engineer", visible_agents=json.dumps(["pyxl"]))
    assert visible_modes(session, user) == ["ticket"]


def test_visible_agents_preserves_grant_order_not_preference_order(session):
    user = _User(role="engineer", visible_agents=json.dumps(["engineer", "ticket"]))
    assert visible_modes(session, user) == ["ticket", "engineer"]


def test_admin_visible_agents_still_narrows(session):
    user = _User(role="engineer", is_admin=True, visible_agents=json.dumps(["pyxl"]))
    assert visible_modes(session, user) == ["pyxl"]
