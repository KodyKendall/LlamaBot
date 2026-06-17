"""Tests for per-instance custom agent-mode overlay loading.

Custom LangGraph agent modes let a Leonardo instance ship its own hyper-specific
agent (its own nodes.py / system prompt / tools / model) and have it appear as a
selectable option in the chat.html mode dropdown WITHOUT rebuilding the image.

The image-level plumbing is: an optional per-instance overlay file
``langgraph/agent_modes.json`` (mounted next to the editable ``langgraph.json``)
declares custom modes; the backend validates them against the registered graphs
and injects them into chat.html. These tests cover the validation core, which is
the brittle part (a typo'd / unregistered agent must NOT appear in the dropdown).

Run with: pytest app/tests/test_custom_agent_modes.py -v
"""
import json

from app.routers.ui import load_custom_agent_modes, BUILTIN_AGENT_MODE_KEYS


GRAPHS = {
    "leo": "./user_agents/leo/nodes.py:build_workflow",
    "research_agent": "./user_agents/research_agent/nodes.py:build_workflow",
}


def _write(tmp_path, payload):
    p = tmp_path / "agent_modes.json"
    if isinstance(payload, str):
        p.write_text(payload)
    else:
        p.write_text(json.dumps(payload))
    return p


def test_missing_file_returns_empty_list(tmp_path):
    # No overlay file → no custom modes (the common case for stock instances).
    missing = tmp_path / "does_not_exist.json"
    assert load_custom_agent_modes(missing, GRAPHS) == []


def test_invalid_json_returns_empty_list(tmp_path):
    p = _write(tmp_path, "{ this is not valid json ")
    assert load_custom_agent_modes(p, GRAPHS) == []


def test_valid_custom_mode_is_loaded_and_sanitized(tmp_path):
    p = _write(tmp_path, [
        {
            "key": "research",
            "label": "Research Mode",
            "agent_name": "research_agent",
            "description": "Web research + contact enrichment",
            "shortLabel": "Research",
        }
    ])
    modes = load_custom_agent_modes(p, GRAPHS)
    assert len(modes) == 1
    m = modes[0]
    assert m["key"] == "research"
    assert m["label"] == "Research Mode"
    assert m["agent_name"] == "research_agent"
    assert m["description"] == "Web research + contact enrichment"
    assert m["shortLabel"] == "Research"


def test_shortlabel_defaults_to_label(tmp_path):
    p = _write(tmp_path, [
        {"key": "research", "label": "Research Mode", "agent_name": "research_agent"}
    ])
    modes = load_custom_agent_modes(p, GRAPHS)
    assert modes[0]["shortLabel"] == "Research Mode"
    assert modes[0]["description"] == ""


def test_unregistered_agent_name_is_dropped(tmp_path):
    # agent_name must exist in langgraph.json graphs, else it can never route —
    # so it must not appear in the dropdown (this is the real failure mode the
    # task calls out: a custom mode that silently 404s the graph).
    p = _write(tmp_path, [
        {"key": "ghost", "label": "Ghost Mode", "agent_name": "not_registered"}
    ])
    assert load_custom_agent_modes(p, GRAPHS) == []


def test_builtin_key_cannot_be_shadowed(tmp_path):
    # Back-compat: a custom entry must never override a built-in mode key.
    assert "engineer" in BUILTIN_AGENT_MODE_KEYS
    p = _write(tmp_path, [
        {"key": "engineer", "label": "Hijacked", "agent_name": "research_agent"},
        {"key": "research", "label": "Research", "agent_name": "research_agent"},
    ])
    modes = load_custom_agent_modes(p, GRAPHS)
    keys = [m["key"] for m in modes]
    assert "engineer" not in keys
    assert keys == ["research"]


def test_entries_missing_required_fields_are_dropped(tmp_path):
    p = _write(tmp_path, [
        {"label": "No key", "agent_name": "research_agent"},
        {"key": "nolabel", "agent_name": "research_agent"},
        {"key": "noagent", "label": "No agent"},
        {"key": "", "label": "Empty key", "agent_name": "research_agent"},
        {"key": "good", "label": "Good", "agent_name": "research_agent"},
    ])
    modes = load_custom_agent_modes(p, GRAPHS)
    assert [m["key"] for m in modes] == ["good"]


def test_duplicate_keys_first_wins(tmp_path):
    p = _write(tmp_path, [
        {"key": "research", "label": "First", "agent_name": "research_agent"},
        {"key": "research", "label": "Second", "agent_name": "leo"},
    ])
    modes = load_custom_agent_modes(p, GRAPHS)
    assert len(modes) == 1
    assert modes[0]["label"] == "First"


def test_non_list_payload_returns_empty(tmp_path):
    p = _write(tmp_path, {"key": "research", "label": "x", "agent_name": "research_agent"})
    assert load_custom_agent_modes(p, GRAPHS) == []


# =============================================================================
# Integration: the chat ("/") route injects custom modes into the HTML and
# extends the engineer-role default visible_agents so the option survives the
# frontend visibility filter. Loader is patched so this doesn't depend on the
# container's cwd langgraph.json / agent_modes.json.
# =============================================================================

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.models import User


@pytest.fixture
def authed_client():
    from main import app

    user = User(id=7, username="leo-modes", password_hash="h", role="engineer",
                is_admin=False, is_active=True)

    def fake_auth(session, username, password):
        return user if (username == user.username and password == "pw") else None

    patches = [
        patch("app.routers.ui.authenticate_user", side_effect=fake_auth),
        patch("app.routers.ui.get_user_by_username", side_effect=lambda s, u: user if u == user.username else None),
        patch("app.dependencies.authenticate_user", side_effect=fake_auth),
        patch("app.dependencies.get_user_by_id", side_effect=lambda s, i: user if i == user.id else None),
    ]
    for p in patches:
        p.start()
    from app.dependencies import get_db_session
    app.dependency_overrides[get_db_session] = lambda: iter([None])
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.post("/login", json={"username": user.username, "password": "pw"})
            yield client
    finally:
        for p in patches:
            p.stop()
        app.dependency_overrides.pop(get_db_session, None)


def test_chat_route_injects_custom_modes_and_extends_visible_agents(authed_client):
    custom = [{"key": "research", "label": "Research Mode", "agent_name": "research_agent",
               "description": "", "shortLabel": "Research", "icon": None}]
    with patch("app.routers.ui.load_custom_agent_modes", return_value=custom):
        resp = authed_client.get("/")
    assert resp.status_code == 200
    html = resp.text
    # Custom modes global is injected for the frontend to render.
    assert "window.LLAMABOT_CUSTOM_AGENT_MODES" in html
    assert "research_agent" in html
    # The custom key is added to the engineer-role default visible_agents so the
    # dynamically-added <option> survives the chat.html visibility filter.
    import re
    m = re.search(r"window\.LLAMABOT_VISIBLE_AGENTS = (\[.*?\]);", html)
    assert m, "visible agents global not found"
    assert "research" in json.loads(m.group(1))
    # Built-in modes are untouched (back-compat).
    assert "engineer" in json.loads(m.group(1))


def test_chat_route_no_custom_modes_is_backcompat(authed_client):
    with patch("app.routers.ui.load_custom_agent_modes", return_value=[]):
        resp = authed_client.get("/")
    assert resp.status_code == 200
    assert "window.LLAMABOT_CUSTOM_AGENT_MODES = []" in resp.text
