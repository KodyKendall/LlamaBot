"""
Regression: repair-before-resume silently discarded the user's question answer.

Shipped broken in 0.7.1 to the whole fleet. Every `ask_user_question` /
`ask_user_uiux_question` card click lost the answer and the agent re-asked the
same question 2-3 times.

The sequence (all in `app/websocket/request_handler.py`):

  1. The plan-mode agent commits an AIMessage with an `ask_user_question`
     tool_call and the tool freezes inside `interrupt()`. At that moment the
     tool_call has NO ToolMessage yet — that is the legitimate pending-interrupt
     state, not corruption.
  2. `handle_question_response` called `_repair_thread_state_if_needed`
     unconditionally BEFORE `astream(Command(resume=answer))`.
  3. The repair's shape-B pass (dangling tool_calls) had no pending-interrupt
     awareness, so it classified the paused tool_call as dangling and wrote a
     synthetic `[Cancelled]` ToolMessage via `aupdate_state`.
  4. That state write superseded the pending interrupt, so the `Command(resume=)`
     that followed landed on a thread with no interrupt — the answer was dropped
     on the floor and the model saw `[Cancelled]` where the answer should be.

0.7.0 had the same unconditional call, but the repair write failed silently
(caught by the blanket `except`); the 0.7.1 `as_node=` fix made the write
succeed, which armed the mis-detection.

The fix is a guard inside `_repair_thread_state_if_needed`: a graph paused on an
interrupt is not corrupted by definition, so leave it alone. The main-chat path
opts OUT (`preserve_pending_interrupts=False`) because there, cancelling a
pending `browser_command` tool_call is the deliberate behaviour — see the
`_QUESTION_INTERRUPT_TYPES` comment.
"""
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from starlette.websockets import WebSocketState

from app.websocket.request_handler import RequestHandler


# ---------------------------------------------------------------------------
# a real paused graph — the only way to prove the answer survives end-to-end
# ---------------------------------------------------------------------------

class _State(TypedDict):
    messages: Annotated[list, "add_messages"]


def _build_graph(interrupt_payload, answer_prefix):
    """START -> model -> tools -> END, where `tools` freezes in interrupt().

    Mirrors the real shape: the AIMessage with the tool_call is committed, then
    the tool blocks, so the checkpoint legitimately holds an unanswered
    tool_call while the graph is paused.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.types import interrupt

    class S(TypedDict):
        messages: Annotated[list, add_messages]

    def model(state):
        return {"messages": [AIMessage(content="", id="ai_1", tool_calls=[
            {"name": "ask_user_question", "id": "call_1", "args": {"question": "Which?"}},
        ])]}

    def tools(state):
        value = interrupt(interrupt_payload)
        return {"messages": [ToolMessage(
            content=f"{answer_prefix}{value}",
            tool_call_id="call_1",
            name="ask_user_question",
        )]}

    g = StateGraph(S)
    g.add_node("model", model)
    g.add_node("tools", tools)
    g.add_edge(START, "model")
    g.add_edge("model", "tools")
    g.add_edge("tools", END)
    return g.compile(checkpointer=MemorySaver())


def _connected_websocket():
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


async def _pause_on_interrupt(graph, config):
    await graph.ainvoke({"messages": [HumanMessage(content="build me a thing")]}, config)
    snap = await graph.aget_state(config)
    assert any(t.interrupts for t in snap.tasks), "graph did not pause on the interrupt"
    return snap


def _contents(snap):
    return [getattr(m, "content", "") for m in snap.values["messages"]]


@pytest.mark.asyncio
async def test_question_answer_is_not_replaced_by_a_cancelled_toolmessage():
    """The bug, end to end: click an option, and the answer must reach the tool."""
    graph = _build_graph(
        {"type": "user_question", "question": "Which layout?", "options": ["A", "B"]},
        "User answered: ",
    )
    config = {"configurable": {"thread_id": "t_question"}}
    await _pause_on_interrupt(graph, config)

    handler = RequestHandler(MagicMock())
    with patch.object(handler, "get_langgraph_app_and_state", return_value=(graph, {}, {})), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        await handler.handle_question_response(
            {"thread_id": "t_question", "agent_name": "rails_plan_mode_agent", "answer": "Option A"},
            _connected_websocket(),
        )

    contents = _contents(await graph.aget_state(config))
    assert "User answered: Option A" in contents, (
        f"the user's answer never reached the tool — state is {contents!r}. "
        "The repair superseded the pending interrupt, so Command(resume=) had "
        "nothing to resume and the answer was silently discarded."
    )
    assert not any("[Cancelled]" in c for c in contents), (
        f"a synthetic [Cancelled] ToolMessage replaced the answer: {contents!r}"
    )


@pytest.mark.asyncio
async def test_approval_decision_is_not_replaced_by_a_cancelled_toolmessage():
    """`handle_approval_response` repairs before resuming too — same clobber."""
    graph = _build_graph(
        {"action_requests": [{"name": "bash_command", "args": {"cmd": "ls"}}]},
        "User decided: ",
    )
    config = {"configurable": {"thread_id": "t_approval"}}
    await _pause_on_interrupt(graph, config)

    handler = RequestHandler(MagicMock())
    with patch.object(handler, "get_langgraph_app_and_state", return_value=(graph, {}, {})), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        await handler.handle_approval_response(
            {"thread_id": "t_approval", "agent_name": "rails_agent",
             "decisions": [{"type": "accept"}]},
            _connected_websocket(),
        )

    contents = _contents(await graph.aget_state(config))
    assert any("User decided:" in c for c in contents), (
        f"the HITL decision never reached the tool — state is {contents!r}"
    )
    assert not any("[Cancelled]" in c for c in contents), (
        f"a synthetic [Cancelled] ToolMessage replaced the decision: {contents!r}"
    )


# ---------------------------------------------------------------------------
# the guard itself, and what it must NOT swallow
# ---------------------------------------------------------------------------

class _App:
    """Records whether the repair actually wrote to state."""

    def __init__(self, messages, tasks=()):
        self._messages = list(messages)
        self._tasks = list(tasks)
        self.updated_with = None

    def get_graph(self):
        return MagicMock(nodes=("__start__", "model", "tools", "__end__"))

    async def aget_state(self, config):
        snap = MagicMock()
        snap.values = {"messages": self._messages}
        snap.tasks = self._tasks
        return snap

    async def aupdate_state(self, config, update, as_node=None):
        self.updated_with = update


def _task_with_interrupt(value):
    intr = MagicMock()
    intr.value = value
    task = MagicMock()
    task.interrupts = [intr]
    return task


def _paused_history():
    return [
        HumanMessage(content="build the thing", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[
            {"name": "ask_user_question", "id": "call_1", "args": {"question": "?"}},
        ]),
    ]


def _handler():
    return RequestHandler.__new__(RequestHandler)


@pytest.mark.asyncio
async def test_repair_leaves_an_interrupt_paused_thread_alone():
    """A paused graph is not corrupted — the unanswered tool_call is the pause."""
    app = _App(_paused_history(), tasks=[_task_with_interrupt(
        {"type": "user_question", "question": "Which?", "options": []}
    )])
    await _handler()._repair_thread_state_if_needed(app, {"configurable": {"thread_id": "t"}})
    assert app.updated_with is None, (
        "repair wrote to a thread that was merely paused — that write supersedes "
        "the pending interrupt and the next Command(resume=) is discarded"
    )


@pytest.mark.asyncio
async def test_repair_still_cancels_a_genuinely_dangling_tool_call():
    """No pending interrupt => the task really was cancelled mid-execution."""
    app = _App(_paused_history(), tasks=[])
    await _handler()._repair_thread_state_if_needed(app, {"configurable": {"thread_id": "t"}})
    assert app.updated_with is not None, "a genuinely bricked thread must still be repaired"
    injected = [m for m in app.updated_with["messages"] if isinstance(m, ToolMessage)]
    assert injected and "[Cancelled]" in injected[0].content


@pytest.mark.asyncio
async def test_main_chat_path_can_still_cancel_a_pending_browser_command():
    """`browser_command` interrupts are answered by the frontend, never by chat text.

    When the user types into the main chat box while one is pending, that path
    deliberately cancels the dangling tool_call rather than feeding the text in
    as a fake browser result — so it opts out of the guard.
    """
    app = _App(_paused_history(), tasks=[_task_with_interrupt(
        {"type": "browser_command", "command": "navigate", "args": {"path": "/users"}}
    )])
    await _handler()._repair_thread_state_if_needed(
        app, {"configurable": {"thread_id": "t"}}, preserve_pending_interrupts=False,
    )
    assert app.updated_with is not None, (
        "the main-chat path must still be able to cancel a pending browser_command"
    )


def test_both_resume_handlers_keep_the_guard_on():
    """The guard is only useful if the two resume call sites do not opt out."""
    import inspect

    src = inspect.getsource(RequestHandler)
    for name in ("handle_question_response", "handle_approval_response"):
        body = src.split(f"async def {name}(")[1].split("\n    async def ")[0]
        assert "preserve_pending_interrupts=False" not in body, (
            f"{name} opts out of the pending-interrupt guard — that is the 0.7.1 bug"
        )
