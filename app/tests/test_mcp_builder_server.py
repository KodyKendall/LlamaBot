"""LP-50: the builder as an MCP server at /mcp.

Plumbing only. The agent itself is replaced by a fake ``handle_request`` that
writes the same frames the real one does, so these tests cover auth, the
off switch, every tool and the result shapes without an LLM or a database.

Run: docker exec -w /app/app leonardo-llamabot-1 python -m pytest tests/test_mcp_builder_server.py -q
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import User
from app.websocket.request_handler import RequestHandler

SECRET = "test-box-login-secret"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


# --------------------------------------------------------------------------- helpers

def mint(secret=SECRET, username="leo-owner", exp_offset=300, aud="llamabot_mcp"):
    """Mint the mothership token with stdlib only — the wire format, not our helper."""
    claims = {"username": username, "exp": int(time.time()) + exp_offset}
    if aud is not None:
        claims["aud"] = aud
    p64 = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), p64.encode(), hashlib.sha256).digest()
    return f"{p64}." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def rpc(client, method, params=None, token=None, rid=1):
    headers = dict(ACCEPT)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers, json=body)


def call(client, tool, args=None, token="default"):
    token = mint() if token == "default" else token
    r = rpc(client, "tools/call", {"name": tool, "arguments": args or {}}, token=token)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    text = result["content"][0]["text"] if result.get("content") else ""
    if result.get("isError"):
        return {"__error__": text}
    return json.loads(text)


OWNER = User(id=7, username="leo-owner", password_hash="x", role="engineer",
             is_admin=True, is_active=True)


@pytest.fixture
def mcp_env(monkeypatch):
    """A minimal app with only /mcp wired, a fake agent, and no database."""
    from app import mcp_server
    from app.services import mcp_auth

    monkeypatch.setenv("LLAMAPRESS_AI_LOGIN_SECRET", SECRET)
    monkeypatch.delenv("LLAMABOT_MCP_ENABLED", raising=False)
    monkeypatch.setattr(mcp_auth, "_policy", lambda: {})
    monkeypatch.setattr(mcp_auth, "_zdr_enforced", lambda: False)
    monkeypatch.setattr(mcp_auth, "_lookup_user", lambda username: OWNER if username == "leo-owner" else None)
    monkeypatch.setattr(mcp_server, "_thread_allowed", lambda principal, thread_id: thread_id != "someone-elses")
    monkeypatch.setattr(mcp_server, "_custom_modes", lambda: {})
    monkeypatch.setattr(mcp_server, "_allowed_modes", lambda principal, custom: ["engineer", "chat"])
    mcp_auth.reset_rate_limits()

    state = {"frames": [], "resumes": [], "gate": None}

    async def fake_handle_request(self, message, sink, task_notification=False):
        state["frames"].append(dict(message))
        behaviour = message.get("message", "")
        if behaviour.startswith("SLOW"):
            state["gate"] = asyncio.Event()
            await sink.send_json({"type": "ai", "content": "working on it", "tool_calls": [],
                                  "base_message": {"tool_calls": [{"name": "write_todos", "args": {"todos": [{"content": "step one", "status": "in_progress"}]}}]}})
            await state["gate"].wait()
        if behaviour.startswith("APPROVE"):
            await sink.send_json({"type": "ai", "content": "", "base_message": {"tool_calls": [{"name": "edit_file", "args": {"file_path": "app/views/home.html.erb"}}]}})
            await sink.send_json({"type": "approval_request", "agent_name": message.get("agent_name"),
                                  "action_requests": [{"name": "edit_file", "args": {"file_path": "app/views/home.html.erb"}, "description": "edit"}]})
            await sink.send_json({"type": "end"})
            return
        if behaviour.startswith("HUGE"):
            for i in range(200):
                await sink.send_json({"type": "tool", "content": "x" * 5000, "base_message": {"name": f"read_file_{i}"}})
        await sink.send_json({"type": "ai", "content": f"Done: {behaviour}", "tool_calls": [], "base_message": {}})
        await sink.send_json({"type": "end"})

    async def fake_resume(self, response_message, sink):
        state["resumes"].append(dict(response_message))
        await sink.send_json({"type": "ai", "content": "Edited after approval", "tool_calls": [], "base_message": {}})
        await sink.send_json({"type": "end"})

    monkeypatch.setattr(RequestHandler, "handle_request", fake_handle_request)
    monkeypatch.setattr(RequestHandler, "handle_approval_response", fake_resume)
    monkeypatch.setattr(RequestHandler, "handle_question_response", fake_resume)

    app = FastAPI()
    mcp_server.register(app)
    with TestClient(app) as client:
        state["client"] = client
        state["app"] = app
        yield state
        if state["gate"] is not None:
            client.portal.call(state["gate"].set)


def wait_done(client, run_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = call(client, "get_run", {"run_id": run_id, "wait_seconds": 1})
        if out.get("status") != "running":
            return out
    raise AssertionError("run never finished")


# --------------------------------------------------------------------------- auth

def test_initialize_answers_without_a_token(mcp_env):
    r = rpc(mcp_env["client"], "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"]


def test_tools_list_without_a_token_is_401_with_resource_metadata(mcp_env):
    r = rpc(mcp_env["client"], "tools/list")
    assert r.status_code == 401
    header = r.headers["www-authenticate"]
    assert header.startswith("Bearer ")
    assert 'resource_metadata="' in header
    assert "/.well-known/oauth-protected-resource" in header


def test_protected_resource_metadata_is_bearer_only(mcp_env):
    r = mcp_env["client"].get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    doc = r.json()
    assert doc["resource"].endswith("/mcp")
    assert doc["bearer_methods_supported"] == ["header"]
    assert "authorization_servers" not in doc


def test_a_token_signed_for_another_box_is_refused(mcp_env):
    r = rpc(mcp_env["client"], "tools/list", token=mint(secret="another-box-secret"))
    assert r.status_code == 401


def test_an_expired_token_is_refused(mcp_env):
    r = rpc(mcp_env["client"], "tools/list", token=mint(exp_offset=-600))
    assert r.status_code == 401


def test_a_login_link_token_is_not_an_mcp_token(mcp_env):
    # The /login?token= magic link has no aud. It must not open /mcp.
    r = rpc(mcp_env["client"], "tools/list", token=mint(aud=None))
    assert r.status_code == 401


def test_an_mcp_token_is_not_a_login_link(monkeypatch):
    from app.services.magic_link_service import MagicLinkInvalid, verify_magic_link_token
    monkeypatch.setenv("LLAMAPRESS_AI_LOGIN_SECRET", SECRET)
    with pytest.raises(MagicLinkInvalid):
        verify_magic_link_token(mint())
    assert verify_magic_link_token(mint(aud=None)) == "leo-owner"


def test_an_mcp_token_lifetime_is_capped(mcp_env):
    r = rpc(mcp_env["client"], "tools/list", token=mint(exp_offset=48 * 3600))
    assert r.status_code == 401


def test_an_unknown_user_is_refused(mcp_env):
    r = rpc(mcp_env["client"], "tools/list", token=mint(username="nobody"))
    assert r.status_code == 401


def test_the_box_ws_jwt_is_accepted(mcp_env):
    from app.services.token_service import create_ws_token
    r = rpc(mcp_env["client"], "tools/list", token=create_ws_token(OWNER))
    assert r.status_code == 200


def test_tools_list_with_a_valid_token(mcp_env):
    r = rpc(mcp_env["client"], "tools/list", token=mint())
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {"send_message", "get_run", "answer", "cancel_run",
                          "list_threads", "get_thread", "list_agent_modes"}
    read_only = {n for n, t in tools.items() if (t.get("annotations") or {}).get("readOnlyHint")}
    assert read_only == {"get_run", "list_threads", "get_thread", "list_agent_modes"}


def test_rate_limited_per_token(mcp_env, monkeypatch):
    monkeypatch.setenv("LLAMABOT_MCP_RATE_LIMIT_PER_MINUTE", "3")
    token = mint()
    codes = [rpc(mcp_env["client"], "tools/list", token=token).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


# --------------------------------------------------------------------------- off switch

@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_env_switch_off_is_404(mcp_env, monkeypatch, value):
    monkeypatch.setenv("LLAMABOT_MCP_ENABLED", value)
    assert rpc(mcp_env["client"], "initialize", {}).status_code == 404
    assert rpc(mcp_env["client"], "tools/list", token=mint()).status_code == 404
    assert mcp_env["client"].get("/.well-known/oauth-protected-resource").status_code == 404


@pytest.mark.parametrize("policy", [{"mcp_enabled": False},
                                    {"instance_overrides": {"mcp_enabled": False}}])
def test_mothership_policy_switches_it_off(mcp_env, monkeypatch, policy):
    from app.services import mcp_auth
    monkeypatch.setattr(mcp_auth, "_policy", lambda: policy)
    assert rpc(mcp_env["client"], "tools/list", token=mint()).status_code == 404
    assert mcp_env["frames"] == []


def test_zdr_box_is_off_by_default_but_env_can_opt_in(mcp_env, monkeypatch):
    from app.services import mcp_auth
    monkeypatch.setattr(mcp_auth, "_zdr_enforced", lambda: True)
    assert rpc(mcp_env["client"], "tools/list", token=mint()).status_code == 404
    monkeypatch.setenv("LLAMABOT_MCP_ENABLED", "true")
    assert rpc(mcp_env["client"], "tools/list", token=mint()).status_code == 200


def test_policy_store_keeps_the_mcp_switch(tmp_path, monkeypatch):
    from app.services import model_policy_store
    monkeypatch.setenv("MODEL_POLICY_PATH", str(tmp_path / "p.json"))
    monkeypatch.setattr(model_policy_store, "_sync_zdr_lock", lambda policy: None)
    model_policy_store.save({"mcp_enabled": False})
    assert model_policy_store.load() == {"mcp_enabled": False}


# --------------------------------------------------------------------------- tools

def test_send_message_runs_the_builder_like_a_ws_chat_frame(mcp_env):
    out = call(mcp_env["client"], "send_message", {
        "message": "Add a footer that says hello", "agent_mode": "engineer", "wait_seconds": 5})
    assert out["thread_id"] and out["run_id"]
    assert out["status"] == "done"
    assert "Done: Add a footer" in out["text_so_far"]
    frame = mcp_env["frames"][0]
    assert frame["agent_name"] == "rails_agent"
    assert frame["thread_id"] == out["thread_id"]
    assert frame["message"] == "Add a footer that says hello"
    # The run lives in the same RunManager the browser attaches to.
    rm = mcp_env["app"].state.run_manager
    assert rm.get(out["thread_id"]) is not None

    run = call(mcp_env["client"], "get_run", {"run_id": out["run_id"]})
    assert run["status"] == "done"
    assert "Done: Add a footer" in run["text"]
    assert isinstance(run["events_cursor"], int)


def test_send_message_returns_running_then_get_run_finishes(mcp_env):
    c = mcp_env["client"]
    out = call(c, "send_message", {"message": "SLOW build", "wait_seconds": 0})
    assert out["status"] == "running"
    run = call(c, "get_run", {"run_id": out["run_id"], "wait_seconds": 0})
    assert run["status"] == "running"
    assert any(s["type"] == "todo_update" and "step one" in s["detail"] for s in run["steps"])
    c.portal.call(mcp_env["gate"].set)
    done = wait_done(c, out["run_id"])
    assert done["status"] == "done"


def test_a_message_mid_run_is_queued_not_a_cancel(mcp_env):
    c = mcp_env["client"]
    first = call(c, "send_message", {"message": "SLOW build", "wait_seconds": 0})
    second = call(c, "send_message", {"message": "also add a header",
                                      "thread_id": first["thread_id"], "wait_seconds": 0})
    assert second["status"] == "queued"
    assert second["thread_id"] == first["thread_id"]
    c.portal.call(mcp_env["gate"].set)
    wait_done(c, second["run_id"])
    assert [f["message"] for f in mcp_env["frames"]] == ["SLOW build", "also add a header"]


def test_cancel_run(mcp_env):
    c = mcp_env["client"]
    out = call(c, "send_message", {"message": "SLOW build", "wait_seconds": 0})
    assert call(c, "cancel_run", {"run_id": out["run_id"]})["cancelled"] is True
    assert call(c, "get_run", {"run_id": out["run_id"], "wait_seconds": 0})["status"] == "cancelled"
    mcp_env["gate"] = None


def test_approval_flow(mcp_env):
    c = mcp_env["client"]
    out = call(c, "send_message", {"message": "APPROVE edit", "ask_before_edits": True, "wait_seconds": 5})
    assert out["status"] == "waiting_for_approval"
    assert mcp_env["frames"][0]["ask_before_edits"] is True
    run = call(c, "get_run", {"run_id": out["run_id"]})
    assert run["status"] == "waiting_for_approval"
    assert run["pending"]["action_requests"][0]["name"] == "edit_file"

    ans = call(c, "answer", {"run_id": out["run_id"], "approval": "approve"})
    assert ans["status"] in ("running", "done")
    done = wait_done(c, ans["run_id"])
    assert done["status"] == "done"
    assert "Edited after approval" in done["text"]
    resume = mcp_env["resumes"][0]
    assert resume["decisions"] == [{"type": "approve"}]
    assert resume["agent_name"] == "rails_agent"
    assert resume["thread_id"] == out["thread_id"]


def test_answer_refuses_when_nothing_is_waiting(mcp_env):
    c = mcp_env["client"]
    out = call(c, "send_message", {"message": "plain", "wait_seconds": 5})
    assert "__error__" in call(c, "answer", {"run_id": out["run_id"], "approval": "approve"})


def test_a_mode_this_principal_lacks_is_refused(mcp_env):
    out = call(mcp_env["client"], "send_message", {"message": "hi", "agent_mode": "database"})
    assert "__error__" in out
    assert mcp_env["frames"] == []


def test_another_users_thread_is_refused(mcp_env):
    c = mcp_env["client"]
    assert "__error__" in call(c, "send_message", {"message": "hi", "thread_id": "someone-elses"})
    assert "__error__" in call(c, "get_run", {"run_id": "someone-elses@0"})
    assert "__error__" in call(c, "get_thread", {"thread_id": "someone-elses"})
    assert mcp_env["frames"] == []


def test_results_stay_under_the_size_cap_and_page(mcp_env):
    c = mcp_env["client"]
    out = call(c, "send_message", {"message": "HUGE", "wait_seconds": 5})
    first = call(c, "get_run", {"run_id": out["run_id"]})
    assert len(json.dumps(first)) < 12_500
    assert first["more"] is True
    second = call(c, "get_run", {"run_id": out["run_id"], "after_event": first["events_cursor"]})
    assert second["events_cursor"] > first["events_cursor"]
    assert len(json.dumps(second)) < 12_500


def test_list_agent_modes(mcp_env):
    out = call(mcp_env["client"], "list_agent_modes")
    assert out["modes"] == [{"key": "engineer", "agent_name": "rails_agent"},
                            {"key": "chat", "agent_name": "rails_plain_chat_mode"}]


def test_list_threads_and_get_thread(mcp_env, monkeypatch):
    from app import mcp_server
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setattr(mcp_server, "_thread_rows", lambda principal, limit: [
        {"thread_id": "t1", "title": "Footer", "updated_at": "2026-11-16T00:00:00Z",
         "message_count": 2, "agent_name": "rails_agent"}])

    async def fake_history(self, thread_id):
        return {"messages": [HumanMessage("add a footer"), AIMessage("done")]}

    monkeypatch.setattr(RequestHandler, "get_chat_history", fake_history)
    c = mcp_env["client"]
    assert call(c, "list_threads", {"limit": 5})["threads"][0]["thread_id"] == "t1"
    thread = call(c, "get_thread", {"thread_id": "t1", "limit": 10})
    assert thread["messages"] == [{"role": "human", "text": "add a footer"},
                                  {"role": "ai", "text": "done"}]


def test_main_app_serves_mcp_and_still_serves_ws():
    from main import app
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/mcp" in paths
    assert "/ws" in paths
    assert "/.well-known/oauth-protected-resource" in paths


def test_get_run_long_polls_instead_of_returning_at_once(mcp_env):
    # Live smoke 2026-11-16: get_run with no cursor returned in 1 ms while the run
    # was still working, because the run's own bookkeeping frames counted as "new".
    c = mcp_env["client"]
    out = call(c, "send_message", {"message": "SLOW build", "wait_seconds": 0})
    first = call(c, "get_run", {"run_id": out["run_id"], "wait_seconds": 0})
    started = time.time()
    again = call(c, "get_run", {"run_id": out["run_id"], "wait_seconds": 1})
    assert time.time() - started >= 0.9
    assert again["status"] == "running"
    started = time.time()
    call(c, "get_run", {"run_id": out["run_id"], "after_event": first["events_cursor"], "wait_seconds": 1})
    assert time.time() - started >= 0.9
