"""delegate_task runs in the background, like delegate_research (0.7.16).

Before: delegate_task (the code-writing sub-agent) ran inline, so the turn
stayed open until the sub-agent finished and the user was stuck waiting.

Now:
- delegate_task hands the work to the thread's RunManager, returns "Task
  started" at once, and the report comes back as a new turn.
- Only one writer at a time: while a coding task runs, the main agent's
  file-writing tools wait for it to finish (box-wide — every thread edits the
  same files), and a checkpoint can't be saved half-way through its edits.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from app.websocket.run_manager import RunManager


def _runtime():
    return SimpleNamespace(
        tool_call_id="call-t",
        state={"llm_model": "deepseek-v4-flash"},
        config={"configurable": {"thread_id": "t1"}},
        stream_writer=lambda _: None,
    )


# ------------------------------------------------------------ delegate_task

@pytest.mark.asyncio
async def test_delegate_task_returns_started_at_once_when_backgroundable(monkeypatch):
    from app.agents.leonardo.rails_agent import sub_agents
    from app.lib.background_tasks import start_background_launcher

    release = asyncio.Event()

    async def slow_delegation(*args, **kwargs):
        await release.wait()
        return {"messages": [AIMessage(content="Added archived to posts")]}

    monkeypatch.setattr(sub_agents, "create_sub_agent", lambda **_: object())
    monkeypatch.setattr(sub_agents, "run_delegation", slow_delegation)

    launched = {}

    def launcher(*, kind, label, work):
        launched.update(kind=kind, label=label, work=work)
        return "c1"

    start_background_launcher(launcher)
    try:
        command = await asyncio.wait_for(
            sub_agents.delegate_task.coroutine(task_description="add archived", runtime=_runtime()), 1,
        )
    finally:
        start_background_launcher(None)

    message = command.update["messages"][0]
    assert "Task started" in message.content and "c1" in message.content
    assert message.tool_call_id == "call-t"
    assert launched["kind"] == "coding" and launched["label"] == "Coding task"

    release.set()
    report = await launched["work"]()
    assert "[DELEGATED TASK COMPLETED]" in report and "archived" in report


@pytest.mark.asyncio
async def test_delegate_task_still_runs_inline_without_a_launcher(monkeypatch):
    from app.agents.leonardo.rails_agent import sub_agents
    from app.lib.background_tasks import start_background_launcher

    async def delegation(*args, **kwargs):
        return {"messages": [AIMessage(content="done")]}

    monkeypatch.setattr(sub_agents, "create_sub_agent", lambda **_: object())
    monkeypatch.setattr(sub_agents, "run_delegation", delegation)
    start_background_launcher(None)

    command = await sub_agents.delegate_task.coroutine(task_description="x", runtime=_runtime())
    assert "[DELEGATED TASK COMPLETED]" in command.update["messages"][0].content


@pytest.mark.asyncio
async def test_coding_task_report_lands_as_a_new_turn(monkeypatch):
    from app.websocket.request_handler import RequestHandler

    handler = RequestHandler(MagicMock())
    handler.app.state.run_manager = RunManager()
    handler.app.state.mothership_client = None
    turns = []

    async def fake_handle_request(message, sink, task_notification=False):
        turns.append((message["message"], task_notification))

    monkeypatch.setattr(handler, "handle_request", fake_handle_request)

    async def work():
        return "[DELEGATED TASK COMPLETED]\n\nAdded archived to posts"

    launcher = handler._background_launcher({"thread_id": "t1", "agent_name": "rails_agent"})
    task_id = launcher(kind="coding", label="Coding task", work=work)
    for _ in range(100):
        if turns:
            break
        await asyncio.sleep(0.01)
    [(text, is_notification)] = turns
    assert is_notification is True
    assert f"Coding task {task_id} finished" in text and "archived" in text


# ------------------------------------------------------------ writer gate

@pytest.mark.asyncio
async def test_wait_for_writers_blocks_until_coding_tasks_finish_on_any_thread():
    rm = RunManager()
    release = asyncio.Event()

    async def coding():
        await release.wait()

    async def research():
        await asyncio.Event().wait()

    rm.start_background("other-thread", coding, kind="coding", label="Coding task")
    rm.start_background("t1", research, kind="research", label="Research")  # read-only: no wait
    assert rm.writers_busy()

    waiter = asyncio.create_task(rm.wait_for_writers())
    await asyncio.sleep(0.05)
    assert not waiter.done()

    release.set()
    await asyncio.wait_for(waiter, 1)
    assert not rm.writers_busy()
    await rm.cancel("t1")


@pytest.mark.asyncio
async def test_main_agent_file_edit_waits_for_the_coding_task():
    from app.agents.leonardo.agent_factory import WriterGateMiddleware
    from app.lib.background_tasks import start_writer_gate

    order = []
    gate_open = asyncio.Event()

    async def gate():
        order.append("waiting")
        await gate_open.wait()

    @tool
    async def edit_file(path: str) -> str:
        """Edit a file."""
        order.append("edited")
        return "ok"

    @tool
    async def read_file(path: str) -> str:
        """Read a file."""
        order.append("read")
        return "contents"

    class _Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    agent = create_agent(
        _Model(responses=[
            AIMessage(content="", tool_calls=[{"name": "read_file", "id": "r", "args": {"path": "a"}}]),
            AIMessage(content="", tool_calls=[{"name": "edit_file", "id": "e", "args": {"path": "a"}}]),
            AIMessage(content="done"),
        ]),
        tools=[edit_file, read_file], middleware=[WriterGateMiddleware()],
    )
    start_writer_gate(gate)
    try:
        run = asyncio.create_task(agent.ainvoke({"messages": [("user", "edit a")]}))
        await asyncio.sleep(0.2)
        assert order == ["read", "waiting"]  # reads go through; the edit waits
        gate_open.set()
        await asyncio.wait_for(run, 2)
    finally:
        start_writer_gate(None)
    assert order == ["read", "waiting", "edited"]


@pytest.mark.asyncio
async def test_sub_agents_never_wait_on_the_writer_gate():
    # The coding sub-agent IS the writer; it inherits the turn's context and
    # would wait on itself forever.
    from app.agents.leonardo.delegation import run_delegation
    from app.lib.background_tasks import current_writer_gate, start_writer_gate

    async def gate():
        pass

    start_writer_gate(gate)
    seen = {}

    class _SubAgent:
        async def astream(self, input_data, config=None, stream_mode=None):
            seen["gate"] = current_writer_gate()
            yield {"messages": [AIMessage(content="done")]}

    try:
        await run_delegation(_SubAgent(), {"messages": []})
        assert seen["gate"] is None
        assert current_writer_gate() is gate
    finally:
        start_writer_gate(None)


def test_every_leonardo_agent_gets_the_writer_gate():
    from app.agents.leonardo import agent_factory

    captured = {}
    original = agent_factory.create_agent
    agent_factory.create_agent = lambda **kw: captured.update(kw)
    try:
        agent_factory.build_leonardo_agent(model=None, tools=[])
    finally:
        agent_factory.create_agent = original
    assert any(isinstance(m, agent_factory.WriterGateMiddleware) for m in captured["middleware"])


# ------------------------------------------------------------ checkpoints

def test_checkpoint_refused_while_a_coding_task_is_editing(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from app.dependencies import get_current_user
    import app.routers.checkpoints as checkpoints

    created = []
    monkeypatch.setattr(checkpoints.checkpoint_service, "create_checkpoint",
                        lambda **kw: created.append(kw) or {"id": "x"})
    rm = RunManager()
    monkeypatch.setattr(app.state, "run_manager", rm, raising=False)
    app.dependency_overrides[get_current_user] = lambda: MagicMock()
    try:
        client = TestClient(app)
        monkeypatch.setattr(rm, "writers_busy", lambda: True)
        busy = client.post("/api/checkpoints", json={"description": "save"})
        assert busy.status_code == 409 and not created

        monkeypatch.setattr(rm, "writers_busy", lambda: False)
        ok = client.post("/api/checkpoints", json={"description": "save"})
        assert ok.status_code == 201 and created
    finally:
        app.dependency_overrides = {}


def test_http_routes_and_the_chat_socket_share_one_run_manager():
    # Live bug (2026-10-23): uvicorn serves `main:app`, but the /ws route does
    # `from app.main import manager`, so main.py runs twice as two modules. Each
    # copy built its own RunManager: coding tasks lived in the socket's copy and
    # the checkpoint route read an empty one, so a save mid-task committed.
    import main
    import app.main as app_main

    assert main.app.state.run_manager is app_main.app.state.run_manager
