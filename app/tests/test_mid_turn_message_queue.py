"""Leo answers mid-research without cancelling (0.7.16).

Before: a message sent while Leo was working cancelled the running turn and the
in-progress work was thrown away (RunManager.start superseded the run), and
delegate_research blocked the turn until the sub-agent finished.

Now:
- A message sent during a running turn is QUEUED on the thread. A middleware
  injects it before the next model call, so the agent sees both messages; if the
  turn ends first, the queue starts the next turn. Only Stop cancels.
- delegate_research runs in the background (owned by RunManager), returns
  "started" at once, and pushes its findings back as a new turn.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from app.websocket.run_manager import QueuedMessage, RunManager


# ------------------------------------------------------------ RunManager queue

@pytest.mark.asyncio
async def test_message_during_active_run_is_queued_not_cancelled():
    rm = RunManager()
    started, release = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()

    async def long_run(sink):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def factory_for(items):
        return long_run

    first = await rm.submit("t1", QueuedMessage({"message": "build it"}), factory_for)
    await started.wait()
    second = await rm.submit("t1", QueuedMessage({"message": "are you there?"}), factory_for)

    assert first == "started"
    assert second == "queued"
    assert not cancelled.is_set()
    assert not rm.get("t1").task.done()
    assert [i.frame["message"] for i in rm.drain("t1")] == ["are you there?"]
    assert rm.drain("t1") == []  # drained once

    release.set()
    await rm.get("t1").task


@pytest.mark.asyncio
async def test_queued_message_left_at_turn_end_starts_followup_run():
    rm = RunManager()
    release = asyncio.Event()
    seen = []
    followup_ran = asyncio.Event()

    def factory_for(items):
        async def run(sink):
            seen.append([i.frame["message"] for i in items])
            if len(seen) == 1:
                await release.wait()
            else:
                followup_ran.set()
        return run

    await rm.submit("t1", QueuedMessage({"message": "one"}), factory_for)
    await asyncio.sleep(0)
    await rm.submit("t1", QueuedMessage({"message": "two"}), factory_for)
    await rm.submit("t1", QueuedMessage({"message": "three"}), factory_for)
    release.set()  # turn ends without a model call draining the queue

    await asyncio.wait_for(followup_ran.wait(), 2)
    assert seen == [["one"], ["two", "three"]]


@pytest.mark.asyncio
async def test_held_message_waits_for_next_model_call_without_starting_turns():
    # Research results that land while Leo waits on a question card must not
    # answer the card, and must not restart the turn over and over either.
    rm = RunManager()
    runs = []

    def factory_for(items):
        async def run(sink):
            runs.append([i.frame["message"] for i in items])
        return run

    rm.requeue("t1", QueuedMessage({"message": "findings"}, notification=True))
    await rm.submit("t1", QueuedMessage({"message": "answer"}), factory_for)
    await rm.get("t1").task
    await asyncio.sleep(0.05)

    assert runs == [["answer"]]  # no extra turns
    assert [i.frame["message"] for i in rm.drain("t1")] == ["findings"]


@pytest.mark.asyncio
async def test_new_run_announces_itself_so_the_ui_can_show_it():
    # A run the server starts on its own (queued follow-up, research results)
    # has no user click behind it — the client needs a frame to spin up.
    rm = RunManager()

    async def run(sink):
        pass

    handle = await rm.start("t1", run)
    await handle.task
    assert handle.log.since(0)[0]["type"] == "run_started"


@pytest.mark.asyncio
async def test_stop_cancels_run_and_drops_queue_and_background_work():
    rm = RunManager()
    started = asyncio.Event()
    research_cancelled = asyncio.Event()

    async def long_run(sink):
        started.set()
        await asyncio.sleep(10)

    async def research():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            research_cancelled.set()
            raise

    await rm.submit("t1", QueuedMessage({"message": "a"}), lambda items: long_run)
    await started.wait()
    await rm.submit("t1", QueuedMessage({"message": "b"}), lambda items: long_run)
    rm.start_background("t1", research, kind="research", label="Research")
    await asyncio.sleep(0)

    assert await rm.cancel("t1") is True
    assert rm.get("t1").log.status == "cancelled"
    assert rm.drain("t1") == []
    await asyncio.wait_for(research_cancelled.wait(), 2)
    assert rm.background_tasks("t1") == []


# -------------------------------------------------------- background tasks

@pytest.mark.asyncio
async def test_work_started_from_inside_a_graph_runs_as_its_own_top_level_run():
    # Live bug (2026-10-22): research is launched from inside a tool call, so its
    # task inherited LangGraph's parent-run config. The results turn it started
    # then ran "nested" in the old, finished run — the model answered, but not
    # one checkpoint landed on the thread. The user identity must still carry.
    from langchain_core.runnables.config import var_child_runnable_config
    from app.lib.request_context import current_user_id, set_current_user_id

    rm = RunManager()
    seen = {}
    done = asyncio.Event()

    async def turn(sink):
        seen["turn_parent"] = var_child_runnable_config.get()
        seen["turn_user"] = current_user_id()
        done.set()

    async def research():
        seen["research_parent"] = var_child_runnable_config.get()
        await rm.submit("t1", QueuedMessage({"message": "findings"}), lambda items: turn)

    set_current_user_id(42)
    var_child_runnable_config.set({"configurable": {"checkpoint_ns": "model:parent"}})
    rm.start_background("t1", research, kind="research", label="Research")
    await asyncio.wait_for(done.wait(), 2)

    assert seen["research_parent"] is None
    assert seen["turn_parent"] is None
    assert seen["turn_user"] == 42


@pytest.mark.asyncio
async def test_background_task_outlives_turn_and_emits_pill_frames():
    rm = RunManager()
    release = asyncio.Event()

    async def research():
        await release.wait()

    task_id = rm.start_background("t1", research, kind="research", label="Research")
    assert task_id
    [info] = rm.background_tasks("t1")
    assert info["task_id"] == task_id and info["kind"] == "research"

    async def quick_turn(sink):
        await sink.send_json({"type": "end"})

    handle = await rm.start("t1", quick_turn)
    await handle.task
    assert rm.background_tasks("t1")  # still running after the turn ended

    release.set()
    for _ in range(50):
        if not rm.background_tasks("t1"):
            break
        await asyncio.sleep(0.01)
    frames = [f for f in rm.get("t1").log.since(0) if f["type"] == "background_task"]
    assert [f["status"] for f in frames] == ["running", "finished"]
    assert all(f["task_id"] == task_id and f.get("started_at") for f in frames)


@pytest.mark.asyncio
async def test_background_tasks_capped_per_thread():
    rm = RunManager()
    never = asyncio.Event()

    async def research():
        await never.wait()

    ids = [rm.start_background("t1", research, kind="research", label="R") for _ in range(4)]
    assert all(ids[:3]) and ids[3] is None
    await rm.cancel("t1")


# ------------------------------------------------- middleware injection

class _RecordingModel(FakeMessagesListChatModel):
    calls: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.asyncio
async def test_queued_message_injected_before_next_model_call():
    from app.agents.leonardo.agent_factory import QueuedMessagesMiddleware
    from app.lib.turn_inbox import start_turn_inbox

    inbox = []

    @tool
    async def slow_lookup(query: str) -> str:
        """Look something up."""
        # The user types while this tool is running.
        inbox.append(HumanMessage(content="are you there?"))
        return "found it"

    model = _RecordingModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "slow_lookup", "id": "c1", "args": {"query": "x"}}]),
        AIMessage(content="yes, and here is the answer"),
    ])
    model.calls = []
    agent = create_agent(model, tools=[slow_lookup], middleware=[QueuedMessagesMiddleware()])

    def drain():
        out = list(inbox)
        inbox.clear()
        return out

    start_turn_inbox(drain)
    result = await agent.ainvoke({"messages": [HumanMessage(content="build it")]})

    second_call = model.calls[1]
    assert isinstance(second_call[-1], HumanMessage)
    assert second_call[-1].content == "are you there?"
    assert isinstance(second_call[-2], ToolMessage)  # after the tool result: valid protocol
    # Persisted in the thread, not just shown to the model once.
    assert any(isinstance(m, HumanMessage) and m.content == "are you there?" for m in result["messages"])


def test_every_leonardo_agent_gets_the_queue_middleware():
    from app.agents.leonardo import agent_factory

    captured = {}
    original = agent_factory.create_agent
    agent_factory.create_agent = lambda **kw: captured.update(kw)
    try:
        agent_factory.build_leonardo_agent(model=None, tools=[])
    finally:
        agent_factory.create_agent = original
    assert any(isinstance(m, agent_factory.QueuedMessagesMiddleware) for m in captured["middleware"])


@pytest.mark.asyncio
async def test_sub_agents_never_drain_the_parents_queue():
    # The inbox is a ContextVar a delegated sub-agent inherits. A sub-agent must
    # not swallow the user's mid-turn message — it belongs to the main agent.
    from app.agents.leonardo.delegation import run_delegation
    from app.lib.turn_inbox import drain_turn_inbox, start_turn_inbox

    start_turn_inbox(lambda: [HumanMessage(content="for the parent")])
    seen = {}

    class _SubAgent:
        async def astream(self, input_data, config=None, stream_mode=None):
            seen["drained"] = drain_turn_inbox()
            yield {"messages": [AIMessage(content="done")]}

    await run_delegation(_SubAgent(), {"messages": []})
    assert seen["drained"] == []
    assert drain_turn_inbox()[0].content == "for the parent"


# ------------------------------------------------- delegate_research

def _runtime():
    return SimpleNamespace(
        tool_call_id="call-r",
        state={"llm_model": "deepseek-v4-flash"},
        config={"configurable": {"thread_id": "t1"}},
        stream_writer=lambda _: None,
    )


@pytest.mark.asyncio
async def test_delegate_research_returns_started_at_once_when_backgroundable(monkeypatch):
    from app.agents.leonardo.rails_agent import sub_agents
    from app.lib.background_tasks import start_background_launcher

    release = asyncio.Event()

    async def slow_delegation(*args, **kwargs):
        await release.wait()
        return {"messages": [AIMessage(content="Jobs get user_id in app/models/job.rb")]}

    monkeypatch.setattr(sub_agents, "create_research_sub_agent", lambda **_: object())
    monkeypatch.setattr(sub_agents, "run_delegation", slow_delegation)

    launched = {}

    def launcher(*, kind, label, work):
        launched.update(kind=kind, label=label, work=work)
        return "r1"

    start_background_launcher(launcher)
    command = await asyncio.wait_for(
        sub_agents.delegate_research.coroutine(task_description="find job user_id", runtime=_runtime()),
        1,
    )

    message = command.update["messages"][0]
    assert "Research started" in message.content and "r1" in message.content
    assert message.tool_call_id == "call-r"
    assert launched["kind"] == "research"

    release.set()
    findings = await launched["work"]()
    assert "app/models/job.rb" in findings


@pytest.mark.asyncio
async def test_delegate_research_still_runs_inline_without_a_launcher(monkeypatch):
    # Scheduled jobs / API callers have no RunManager to own a background task.
    from app.agents.leonardo.rails_agent import sub_agents
    from app.lib.background_tasks import start_background_launcher

    async def delegation(*args, **kwargs):
        return {"messages": [AIMessage(content="the findings")]}

    monkeypatch.setattr(sub_agents, "create_research_sub_agent", lambda **_: object())
    monkeypatch.setattr(sub_agents, "run_delegation", delegation)
    start_background_launcher(None)

    command = await sub_agents.delegate_research.coroutine(task_description="x", runtime=_runtime())
    assert "[RESEARCH COMPLETED]" in command.update["messages"][0].content


# ------------------------------------------- RequestHandler end to end

def _handler():
    from app.websocket.request_handler import RequestHandler

    handler = RequestHandler(MagicMock())
    handler.app.state.run_manager = RunManager()
    handler.app.state.mothership_client = None
    return handler


@pytest.mark.asyncio
async def test_second_message_mid_turn_does_not_cancel_and_agent_sees_both(monkeypatch):
    """Ticket test 1: queued, not cancelled; the agent sees both messages."""
    handler = _handler()
    calls = []
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_handle_request(message, sink, task_notification=False):
        calls.append(message["message"])
        if len(calls) == 1:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(handler, "handle_request", fake_handle_request)
    frame = {"thread_id": "t1", "agent_name": "rails_agent"}
    await handler.start_chat_run({**frame, "message": "build the page"}, None)
    await asyncio.sleep(0)
    await handler.start_chat_run({**frame, "message": "are you there?"}, None)
    await asyncio.sleep(0.01)
    assert calls == ["build the page"] and not cancelled.is_set()

    release.set()
    for _ in range(100):
        if len(calls) == 2:
            break
        await asyncio.sleep(0.01)
    assert calls == ["build the page", "are you there?"]


@pytest.mark.asyncio
async def test_reply_during_background_research_then_findings_land(monkeypatch):
    """Ticket test 2: a message during research gets a reply before research ends,
    and the findings still arrive afterwards as a new turn."""
    handler = _handler()
    rm = handler._run_manager()
    turns = []

    async def fake_handle_request(message, sink, task_notification=False):
        turns.append((message["message"], task_notification))

    monkeypatch.setattr(handler, "handle_request", fake_handle_request)
    origin = {"thread_id": "t1", "agent_name": "rails_agent", "llm_model": "deepseek-v4-flash",
              "message": "why are jobs on the wrong user?"}
    release = asyncio.Event()

    async def work():
        await release.wait()
        return "Jobs get user_id in app/models/job.rb"

    launcher = handler._background_launcher(origin)
    task_id = launcher(kind="research", label="Research", work=work)
    assert task_id

    await handler.start_chat_run({**origin, "message": "are you there?"}, None)
    await rm.get("t1").task
    assert turns == [("are you there?", False)]
    assert rm.background_tasks("t1")  # research still running

    release.set()
    for _ in range(100):
        if len(turns) == 2 and not rm.background_tasks("t1"):
            break
        await asyncio.sleep(0.01)
    text, is_notification = turns[1]
    assert is_notification is True
    assert task_id in text and "app/models/job.rb" in text
    assert not rm.background_tasks("t1")  # pill clears


@pytest.mark.asyncio
async def test_research_results_turn_is_saved_flagged_so_history_shows_a_marker():
    # Live bug (2026-10-22): the results turn saved its message unflagged, so a
    # reload rendered the raw findings as if the user had typed them.
    from unittest.mock import AsyncMock, patch
    from langgraph.checkpoint.memory import MemorySaver
    from starlette.websockets import WebSocketState

    class _Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    agent = create_agent(_Model(responses=[AIMessage(content="here is what it found")]),
                         tools=[], checkpointer=MemorySaver())
    handler = _handler()
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    note = "<task-notification>Research r1 finished.</task-notification>"
    state = {"messages": [HumanMessage(content=note)]}
    with patch.object(handler, "get_langgraph_app_and_state", return_value=(agent, state, {})), \
         patch.object(handler, "_check_instance_lock_or_block", AsyncMock(return_value=False)), \
         patch.object(handler, "_warn_if_model_substituted", AsyncMock()), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        await handler.handle_request({"thread_id": "t_note", "agent_name": "rails_agent", "message": note},
                                     ws, task_notification=True)

    saved = (await agent.aget_state({"configurable": {"thread_id": "t_note"}})).values["messages"]
    [human] = [m for m in saved if isinstance(m, HumanMessage)]
    assert human.additional_kwargs.get("task_notification") is True


@pytest.mark.asyncio
async def test_message_read_mid_turn_is_not_echoed_back_to_the_browser():
    # Live bug (2026-10-22): "Also say how many models there are", sent while Leo
    # was mid-turn, showed 3 times — once from the browser's own send, plus the
    # state update that added it to the conversation, streamed back (twice) and
    # rendered as new user bubbles.
    from unittest.mock import AsyncMock, patch
    from langgraph.checkpoint.memory import MemorySaver
    from starlette.websockets import WebSocketState
    from app.agents.leonardo.agent_factory import QueuedMessagesMiddleware

    handler = _handler()
    rm = handler._run_manager()

    @tool
    async def slow_lookup(query: str) -> str:
        """Look something up."""
        rm._handle_for("t_echo").pending.append(
            QueuedMessage({"thread_id": "t_echo", "message": "Also say how many models there are"}))
        return "found it"

    class _Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    agent = create_agent(
        _Model(responses=[
            AIMessage(content="", tool_calls=[{"name": "slow_lookup", "id": "c1", "args": {"query": "x"}}]),
            AIMessage(content="There are 2 models."),
        ]),
        tools=[slow_lookup], middleware=[QueuedMessagesMiddleware()], checkpointer=MemorySaver(),
    )
    sent = []
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock(side_effect=lambda m: sent.append(m))
    state = {"messages": [HumanMessage(content="list the models")]}
    with patch.object(handler, "get_langgraph_app_and_state", return_value=(agent, state, {})), \
         patch.object(handler, "_check_instance_lock_or_block", AsyncMock(return_value=False)), \
         patch.object(handler, "_paywall_blocks_turn", AsyncMock(return_value=False)), \
         patch.object(handler, "_warn_if_model_substituted", AsyncMock()), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        await handler.handle_request({"thread_id": "t_echo", "agent_name": "rails_agent", "message": "list the models"}, ws)

    saved = (await agent.aget_state({"configurable": {"thread_id": "t_echo"}})).values["messages"]
    assert any(m.content == "Also say how many models there are" for m in saved)  # the agent got it
    assert not [m for m in sent if m.get("type") == "human"], "queued message echoed back as a user bubble"
