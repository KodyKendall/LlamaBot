"""AI safety limits on ZDR boxes (LP-33): HECVAT AIGN-05, AILM-01, AILM-04.

Everything here is off on a normal box and on under ZDR, with no other switch.
The model and tool hooks sit on the LangChain base classes, so these use real
subclasses rather than stubs: an agent that skips our middleware still goes through.
"""
import asyncio
import logging
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app import permissions
from app.agents.leonardo import llm_factory  # noqa: F401 - installs the hooks
from app.agents.leonardo import sensitive_data, tool_limits, zdr
from app.lib import request_context

SSN = "123-45-6789"
CARD = "4111 1111 1111 1111"
NOT_A_CARD = "4111 1111 1111 1112"
STUDENT_ID = "u1234567"


class CapturingModel(BaseChatModel):
    """Records what would have gone to the vendor."""

    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "capturing"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


@pytest.fixture(autouse=True)
def _no_budget_left_behind():
    """start_turn sets a ContextVar on this thread, which later test files share."""
    token = tool_limits._budget.set(None)
    yield
    tool_limits._budget.reset(token)


@pytest.fixture
def model():
    return CapturingModel(seen=[])


@pytest.fixture
def zdr_on(monkeypatch):
    monkeypatch.setattr(zdr, "enforced", lambda: True)
    monkeypatch.setattr(sensitive_data, "stored_config", lambda: {})
    monkeypatch.setattr(permissions, "role_write_tool_grants", lambda: {})
    permissions.invalidate_tool_grants()
    sensitive_data.reset_for_tests()
    yield
    permissions.invalidate_tool_grants()


@pytest.fixture
def zdr_off(monkeypatch):
    monkeypatch.setattr(zdr, "enforced", lambda: False)
    sensitive_data.reset_for_tests()


def sent_text(model) -> str:
    return " ".join(str(m.content) for m in model.seen[-1])


# ---------------------------------------------------------------------------
# AIGN-05: nothing sensitive reaches a model
# ---------------------------------------------------------------------------

def test_ssn_is_redacted_in_a_user_message(model, zdr_on):
    model.invoke([HumanMessage(content=f"my ssn is {SSN}")])
    assert SSN not in sent_text(model)
    assert "[REDACTED: SSN]" in sent_text(model)


def test_student_id_is_redacted(model, zdr_on):
    model.invoke([HumanMessage(content=f"student {STUDENT_ID} failed")])
    assert STUDENT_ID not in sent_text(model)
    assert "[REDACTED: STUDENT_ID]" in sent_text(model)


def test_card_needs_a_valid_luhn_checksum(model, zdr_on):
    model.invoke([HumanMessage(content=f"good {CARD} bad {NOT_A_CARD}")])
    assert CARD not in sent_text(model)
    assert NOT_A_CARD in sent_text(model)


@pytest.mark.parametrize("call", ["invoke", "ainvoke", "stream", "astream"])
def test_every_way_of_calling_a_model_is_covered(model, zdr_on, call):
    messages = [HumanMessage(content=SSN)]
    if call == "invoke":
        model.invoke(messages)
    elif call == "ainvoke":
        asyncio.run(model.ainvoke(messages))
    elif call == "stream":
        list(model.stream(messages))
    else:
        async def drain():
            return [c async for c in model.astream(messages)]
        asyncio.run(drain())
    assert SSN not in sent_text(model)


def test_tool_results_system_text_and_tool_args_are_covered(model, zdr_on):
    model.invoke([
        SystemMessage(content=f"memory: {SSN}"),
        HumanMessage(content="hi"),
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": SSN}, "id": "1"}]),
        ToolMessage(content=f"row: {SSN}", tool_call_id="1"),
    ])
    assert SSN not in str(model.seen[-1])


def test_text_blocks_are_covered_and_images_kept(model, zdr_on):
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    model.invoke([HumanMessage(content=[{"type": "text", "text": SSN}, image])])
    content = model.seen[-1][0].content
    assert SSN not in str(content)
    assert image in content


def test_an_attached_file_that_cannot_be_scanned_is_withheld(model, zdr_on):
    block = {"type": "file", "source_type": "base64", "mime_type": "application/pdf", "data": "SECRETDATA"}
    model.invoke([HumanMessage(content=[{"type": "text", "text": "read this"}, block])])
    assert "SECRETDATA" not in str(model.seen[-1])
    assert "withheld" in str(model.seen[-1]).lower()


def test_the_original_message_object_is_not_changed(model, zdr_on):
    message = HumanMessage(content=SSN)
    model.invoke([message])
    assert message.content == SSN


def test_a_custom_rule_applies(model, zdr_on, monkeypatch):
    monkeypatch.setattr(sensitive_data, "stored_config", lambda: {
        "custom": [{"name": "badge", "pattern": r"BADGE-\d+", "action": "redact"}],
    })
    sensitive_data.reset_for_tests()
    model.invoke([HumanMessage(content="BADGE-42")])
    assert "BADGE-42" not in sent_text(model)
    assert "[REDACTED: BADGE]" in sent_text(model)


def test_student_id_format_is_configurable(model, zdr_on, monkeypatch):
    monkeypatch.setattr(sensitive_data, "stored_config", lambda: {"student_id_pattern": r"S\d{5}"})
    sensitive_data.reset_for_tests()
    model.invoke([HumanMessage(content=f"S12345 and {STUDENT_ID}")])
    assert "S12345" not in sent_text(model)
    assert STUDENT_ID in sent_text(model)


def test_block_action_stops_the_call_and_never_shows_the_text(model, zdr_on, monkeypatch):
    monkeypatch.setattr(sensitive_data, "stored_config", lambda: {"actions": {"ssn": "block"}})
    sensitive_data.reset_for_tests()
    with pytest.raises(tool_limits.TurnSafetyStop) as stopped:
        model.invoke([HumanMessage(content=SSN)])
    assert model.seen == []
    assert SSN not in str(stopped.value)
    assert "SSN" in str(stopped.value)


def test_a_blocked_message_does_not_wedge_the_thread(model, zdr_on, monkeypatch):
    """It stays in history, so later turns must redact it rather than block forever."""
    monkeypatch.setattr(sensitive_data, "stored_config", lambda: {"actions": {"ssn": "block"}})
    sensitive_data.reset_for_tests()
    model.invoke([HumanMessage(content=SSN), AIMessage(content="sorry"), HumanMessage(content="next")])
    assert SSN not in sent_text(model)


def test_only_counts_are_logged_never_the_text(model, zdr_on, caplog):
    with caplog.at_level(logging.DEBUG):
        model.invoke([HumanMessage(content=f"{SSN} and {SSN}")])
    assert SSN not in caplog.text
    assert sensitive_data.counts() == {"ssn": 2}


def test_rescanning_history_does_not_inflate_the_count(model, zdr_on):
    first = HumanMessage(content=SSN)
    model.invoke([first])
    model.invoke([first, AIMessage(content="ok"), HumanMessage(content="more")])
    assert sensitive_data.counts() == {"ssn": 1}


def test_counts_are_in_the_zdr_report(model, zdr_on, monkeypatch):
    monkeypatch.setattr(zdr, "zdr_state", lambda: zdr.ZDRState(True))
    model.invoke([HumanMessage(content=SSN)])
    assert zdr.report()["sensitive_data_counts"] == {"ssn": 1}


def test_a_bad_stored_rule_is_skipped_and_builtins_still_work(model, zdr_on, monkeypatch):
    monkeypatch.setattr(sensitive_data, "stored_config", lambda: {
        "custom": [{"name": "broken", "pattern": "(", "action": "redact"}],
    })
    sensitive_data.reset_for_tests()
    model.invoke([HumanMessage(content=SSN)])
    assert SSN not in sent_text(model)


def test_unreadable_rules_fall_back_to_the_builtins(model, zdr_on, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(sensitive_data, "stored_config", boom)
    sensitive_data.reset_for_tests()
    model.invoke([HumanMessage(content=SSN)])
    assert SSN not in sent_text(model)


@pytest.mark.parametrize("payload", [
    {"custom": [{"name": "x", "pattern": "(", "action": "redact"}]},
    {"custom": [{"name": "x", "pattern": "a", "action": "explode"}]},
    {"actions": {"ssn": "nope"}},
    {"student_id_pattern": "("},
])
def test_invalid_config_is_rejected_when_saved(payload):
    with pytest.raises(ValueError):
        sensitive_data.validate_config(payload)


def test_standard_box_sends_text_untouched(model, zdr_off):
    model.invoke([HumanMessage(content=SSN)])
    assert SSN in sent_text(model)


# ---------------------------------------------------------------------------
# AILM-01: read-only by default under ZDR
# ---------------------------------------------------------------------------

@tool
def read_file(path: str) -> str:
    """Read."""
    return "contents"


@tool
def write_file(path: str) -> str:
    """Write."""
    return "written"


@tool
def bash_command(command: str) -> str:
    """Shell."""
    return "ran"


@tool
def some_new_tool_nobody_classified(x: str) -> str:
    """A tool added later must not be open by default."""
    return "ran"


@pytest.fixture
def engineer():
    token = request_context.set_current_principal("engineer", False)
    yield
    request_context.reset_current_principal(token)


@pytest.mark.parametrize("name", [
    "write_file", "edit_file", "bash_command", "hard_restart_rails", "fix_permissions",
    "git_commit", "git_command", "delegate_task", "some_new_tool_nobody_classified",
])
def test_write_and_shell_tools_are_not_read_only(name):
    assert name not in permissions.READ_ONLY_TOOLS


def test_loading_skill_instructions_stays_available():
    assert "use_skill" in permissions.READ_ONLY_TOOLS


def test_standard_user_sees_only_read_only_tools(zdr_on, engineer):
    tools = tool_limits.visible_tools([read_file, write_file, bash_command, some_new_tool_nobody_classified])
    assert [t.name for t in tools] == ["read_file"]


def test_a_write_tool_cannot_run_even_if_a_graph_kept_it(zdr_on, engineer):
    with pytest.raises(tool_limits.TurnSafetyStop):
        write_file.invoke({"path": "x"})


def test_read_tool_runs(zdr_on, engineer):
    assert read_file.invoke({"path": "x"}) == "contents"


def test_admin_keeps_every_tool(zdr_on):
    token = request_context.set_current_principal("engineer", True)
    try:
        assert len(tool_limits.visible_tools([read_file, write_file])) == 2
        assert write_file.invoke({"path": "x"}) == "written"
    finally:
        request_context.reset_current_principal(token)


def test_admin_can_grant_write_tools_to_a_role(zdr_on, engineer, monkeypatch):
    monkeypatch.setattr(permissions, "role_write_tool_grants", lambda: {"engineer": True})
    permissions.invalidate_tool_grants()
    assert write_file.invoke({"path": "x"}) == "written"


def test_a_grant_for_one_role_does_not_reach_another(zdr_on, monkeypatch):
    monkeypatch.setattr(permissions, "role_write_tool_grants", lambda: {"engineer": True})
    permissions.invalidate_tool_grants()
    token = request_context.set_current_principal("user", False)
    try:
        assert tool_limits.visible_tools([write_file]) == []
    finally:
        request_context.reset_current_principal(token)


def test_web_search_still_answers_with_its_own_refusal(monkeypatch, zdr_on, engineer):
    from app.agents.leonardo.rails_agent import tools

    monkeypatch.setattr(tools, "get_tavily_client", lambda: pytest.fail("called Tavily on a ZDR box"))
    assert tools.internet_search.invoke({"query": "x"}) == zdr.WEB_SEARCH_DISABLED_MESSAGE


def test_unknown_caller_is_read_only(zdr_on):
    assert tool_limits.visible_tools([write_file]) == []
    with pytest.raises(tool_limits.TurnSafetyStop):
        write_file.invoke({"path": "x"})


def test_grants_that_cannot_be_read_deny(zdr_on, engineer, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(permissions, "role_write_tool_grants", boom)
    permissions.invalidate_tool_grants()
    assert tool_limits.visible_tools([write_file]) == []


def test_standard_box_keeps_every_tool(zdr_off, engineer):
    assert len(tool_limits.visible_tools([read_file, write_file, bash_command])) == 3
    assert write_file.invoke({"path": "x"}) == "written"


def test_shared_agents_hide_write_tools_from_the_model(zdr_on, engineer):
    from app.agents.leonardo.agent_factory import ToolPrivilegeMiddleware

    class Request:
        tools = [read_file, write_file]

        def override(self, **kwargs):
            self.tools = kwargs["tools"]
            return self

    seen = ToolPrivilegeMiddleware().wrap_model_call(Request(), lambda r: r.tools)
    assert [t.name for t in seen] == ["read_file"]


# ---------------------------------------------------------------------------
# AILM-04: a cap per user input
# ---------------------------------------------------------------------------

def test_cap_stops_the_next_call_and_says_so(zdr_on, engineer, monkeypatch):
    monkeypatch.setenv("TOOL_CALL_PER_TURN_LIMIT", "3")
    tool_limits.start_turn()
    for _ in range(3):
        read_file.invoke({"path": "x"})
    with pytest.raises(tool_limits.TurnSafetyStop) as stopped:
        read_file.invoke({"path": "x"})
    assert "limit" in str(stopped.value).lower()
    assert tool_limits.cap_was_hit()


def test_default_cap_is_40(monkeypatch):
    monkeypatch.delenv("TOOL_CALL_PER_TURN_LIMIT", raising=False)
    assert tool_limits.tool_call_limit() == 40


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
def test_a_bad_cap_setting_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("TOOL_CALL_PER_TURN_LIMIT", bad)
    assert tool_limits.tool_call_limit() == 40


def test_a_new_input_gets_a_new_budget(zdr_on, engineer, monkeypatch):
    monkeypatch.setenv("TOOL_CALL_PER_TURN_LIMIT", "1")
    tool_limits.start_turn()
    read_file.invoke({"path": "x"})
    tool_limits.start_turn()
    assert read_file.invoke({"path": "x"}) == "contents"


def test_sub_agent_calls_count_against_the_parent(zdr_on, engineer, monkeypatch):
    """Sub-agents and background work run in tasks copied from the parent's context."""
    monkeypatch.setenv("TOOL_CALL_PER_TURN_LIMIT", "2")

    @tool
    async def delegate_research(prompt: str) -> str:
        """Runs a sub-agent."""
        return await asyncio.create_task(read_file.ainvoke({"path": "x"}))

    async def turn():
        tool_limits.start_turn()
        await delegate_research.ainvoke({"prompt": "go"})  # 1 parent + 1 sub-agent
        await read_file.ainvoke({"path": "y"})

    with pytest.raises(tool_limits.TurnSafetyStop):
        asyncio.run(turn())


def test_cap_is_off_on_a_standard_box(zdr_off, engineer, monkeypatch):
    monkeypatch.setenv("TOOL_CALL_PER_TURN_LIMIT", "1")
    tool_limits.start_turn()
    for _ in range(5):
        read_file.invoke({"path": "x"})
    assert not tool_limits.cap_was_hit()


def test_a_real_agent_loop_halts_cleanly_at_the_cap(zdr_on, engineer, monkeypatch):
    """Through create_agent's ToolNode, the stop must reach the turn as itself."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from app.agents.leonardo.agent_factory import build_leonardo_agent

    class AlwaysCallsATool(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    def calls():
        n = 0
        while True:
            n += 1
            yield AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": str(n)}])

    monkeypatch.setenv("TOOL_CALL_PER_TURN_LIMIT", "3")
    agent = build_leonardo_agent(model=AlwaysCallsATool(messages=calls()), tools=[read_file])

    async def turn():
        tool_limits.start_turn()
        await agent.ainvoke({"messages": [HumanMessage(content="go")]}, {"recursion_limit": 100})

    with pytest.raises(tool_limits.TurnSafetyStop, match="safety limit"):
        asyncio.run(turn())


# ---------------------------------------------------------------------------
# The ZDR flag is the only switch
# ---------------------------------------------------------------------------

def test_a_pushed_zdr_policy_turns_the_filter_on(model, monkeypatch, tmp_path):
    from app.services.lease_manager import LeaseManager

    monkeypatch.setenv("MODEL_POLICY_PATH", str(tmp_path / "model_policy.json"))
    monkeypatch.setattr(sensitive_data, "stored_config", lambda: {})
    sensitive_data.reset_for_tests()
    zdr._reset_for_tests()
    try:
        model.invoke([HumanMessage(content=SSN)])
        assert SSN in sent_text(model)

        policy = {"instance_overrides": {"zdr": {"enabled": True, "allowed_models": []}}}
        LeaseManager._sync_model_policy(object.__new__(LeaseManager), {"model_policy": policy})
        model.invoke([HumanMessage(content=SSN)])
        assert SSN not in sent_text(model)
    finally:
        zdr.clear_lock()
        zdr._reset_for_tests()
        import langsmith
        langsmith.configure(enabled=None)


# ---------------------------------------------------------------------------
# The turn ends cleanly, and the user is told
# ---------------------------------------------------------------------------

async def _run_turn(fake_astream):
    from unittest.mock import AsyncMock, MagicMock, patch
    from starlette.websockets import WebSocketState
    from app.websocket.request_handler import RequestHandler

    handler = RequestHandler(MagicMock())
    handler.app.state.mothership_client = None
    websocket = MagicMock()
    websocket.client_state = websocket.application_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()
    graph = MagicMock()
    graph.astream = fake_astream
    report = AsyncMock()
    with patch.object(handler, "_check_instance_lock_or_block", AsyncMock(return_value=False)), \
         patch.object(handler, "_check_paywall_or_block", AsyncMock(return_value=False)), \
         patch.object(handler, "_report_error_to_mothership", report), \
         patch.object(handler, "_repair_thread_state_if_needed", AsyncMock()), \
         patch.object(handler, "_pending_question_interrupt", AsyncMock(return_value=None)), \
         patch.object(handler, "get_langgraph_app_and_state", return_value=(graph, {"messages": []}, {})):
        await handler.handle_request({"thread_id": "t", "message": "hi", "agent_name": "rails_agent"}, websocket)
    return [c.args[0] for c in websocket.send_json.await_args_list], report


@pytest.mark.asyncio
async def test_hitting_the_cap_ends_the_turn_with_a_message_not_an_error(zdr_on, engineer, monkeypatch):
    monkeypatch.setenv("TOOL_CALL_PER_TURN_LIMIT", "1")

    async def astream(*args, **kwargs):
        read_file.invoke({"path": "x"})
        read_file.invoke({"path": "y"})
        yield None

    frames, report = await _run_turn(astream)
    types = [f["type"] for f in frames]
    assert "error" not in types
    assert types[-1] == "end"
    notices = [f["content"] for f in frames if f["type"] == "system_message"]
    assert any("safety limit" in n for n in notices)
    report.assert_not_called()


@pytest.mark.asyncio
async def test_a_sub_agent_swallowing_the_stop_still_tells_the_user(zdr_on, engineer, monkeypatch):
    """delegate_task turns exceptions into text, so the turn can finish normally."""
    monkeypatch.setenv("TOOL_CALL_PER_TURN_LIMIT", "1")

    async def astream(*args, **kwargs):
        read_file.invoke({"path": "x"})
        try:
            read_file.invoke({"path": "y"})
        except tool_limits.TurnSafetyStop:
            pass
        return
        yield

    frames, _ = await _run_turn(astream)
    assert any("safety limit" in f.get("content", "") for f in frames if f["type"] == "system_message")


# ---------------------------------------------------------------------------
# Admin settings
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    import app.models  # noqa: F401
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from app.dependencies import get_db_session, security_admin_required
    from app.models import User
    from app.routers.api import router

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    api = FastAPI()
    api.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    api.dependency_overrides[get_db_session] = _session
    api.dependency_overrides[security_admin_required] = lambda: User(
        username="admin", password_hash="x", is_admin=True, role="engineer"
    )
    return TestClient(api)


def test_admin_saves_and_reads_sensitive_data_rules(client):
    rules = {"actions": {"ssn": "block"}, "student_id_pattern": r"S\\d{5}",
             "custom": [{"name": "badge", "pattern": "BADGE", "action": "redact"}]}
    assert client.put("/api/sensitive-data-rules", json=rules).status_code == 200
    saved = client.get("/api/sensitive-data-rules").json()
    assert saved["rules"]["actions"] == {"ssn": "block"}
    assert saved["rules"]["custom"][0]["name"] == "badge"
    assert "builtin" in saved


def test_a_bad_rule_is_refused_and_not_stored(client):
    bad = {"custom": [{"name": "x", "pattern": "(", "action": "redact"}]}
    assert client.put("/api/sensitive-data-rules", json=bad).status_code == 400
    assert client.get("/api/sensitive-data-rules").json()["rules"] == {}


def test_admin_grants_write_tools_per_role(client):
    r = client.put("/api/role-write-tools", json={"grants": {"engineer": True, "user": False}})
    assert r.status_code == 200
    assert client.get("/api/role-write-tools").json()["grants"] == {"engineer": True, "user": False}


@pytest.mark.parametrize("body", [{"grants": "yes"}, {"grants": {"engineer": "true"}}, {}])
def test_a_malformed_grant_is_refused(client, body):
    assert client.put("/api/role-write-tools", json=body).status_code == 400


@pytest.mark.parametrize("method,path", [
    ("get", "/api/sensitive-data-rules"), ("put", "/api/sensitive-data-rules"),
    ("get", "/api/role-write-tools"), ("put", "/api/role-write-tools"),
])
def test_these_settings_are_admin_only(method, path):
    from fastapi.routing import APIRoute
    from app.dependencies import security_admin_required
    from app.routers.api import router

    route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path == path and method.upper() in r.methods)
    assert security_admin_required in [d.call for d in route.dependant.dependencies]


# ---------------------------------------------------------------------------
# AILM-06: an approved model needs a recorded eval
# ---------------------------------------------------------------------------

def test_every_zdr_approved_model_has_an_eval_record():
    """Adding a model to ZDR_COMPLIANT fails CI until it is in agents/leonardo/model_evals.json."""
    import json
    from pathlib import Path
    from app.agents.leonardo.model_capabilities import ZDR_COMPLIANT

    records = json.loads((Path(__file__).parents[1] / "agents/leonardo/model_evals.json").read_text())
    approved = {name for name, ok in ZDR_COMPLIANT.items() if ok is True}
    assert approved - set(records) == set()
    for name in approved:
        record = records[name]
        assert record["status"] in ("passed", "pending")
        if record["status"] == "passed":
            assert isinstance(record["pass_rate"], (int, float)) and record["date"]
