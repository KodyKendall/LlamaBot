"""
Regression: answering a question the thread had moved past crashed the turn.

Reported as feedback #52 on leo-rozeze (Raw Lens Media), 14 Sep 2026:

    Error resuming after question: BadRequestError: Error code: 400 -
    {'error': {'message': '`messages` must contain at least one message with
    role `user` or `tool`', ...}}

What happened (llamabot logs, thread 7b08997f, rails_plan_mode_agent): the
plan-mode agent asked two questions in one turn. The first answer resumed
cleanly — "Skipping thread state repair: graph is paused on a pending
interrupt". The second answer, 75s later, did not log that line at all: by then
`aget_state` saw no pending interrupt, so `Command(resume=...)` re-entered the
graph with no input. The request that reached the provider was
`{"message_count": 0, "messages": [], "has_user_or_tool": false}` — hence the
400, surfaced to the user as a red "Error resuming after question".

Two independent holes, both closed here:

1. `handle_question_response` resumed without checking whether there was
   anything to resume. A `Command(resume=...)` only means something while the
   graph is paused inside `interrupt()`; on a thread that has moved on it is a
   no-input re-entry. It now tells the user the question expired and stops.

2. `normalize_messages_for_provider` — the one validator every provider call
   passes through — guarded its "at least one user/tool message" fix with
   `if out and ...`, so a completely EMPTY history skipped the fix and went to
   the provider unmodified. The empty case is the same 400 the guard exists to
   prevent. Covered in `test_message_invariants.py`.
"""
from typing import Annotated, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from starlette.websockets import WebSocketState

from app.websocket.request_handler import (
    QUESTION_ALREADY_ANSWERED_MESSAGE,
    RequestHandler,
)


def _build_graph():
    """START -> model -> tools -> END, where `tools` pauses once in interrupt()."""
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
        value = interrupt({"type": "user_question", "question": "Which?", "options": ["A", "B"]})
        return {"messages": [ToolMessage(
            content=f"User answered: {value}",
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


def _sent_types(ws):
    return [call.args[0].get("type") for call in ws.send_json.call_args_list]


async def _answer(handler, graph, ws, answer):
    with patch.object(handler, "get_langgraph_app_and_state", return_value=(graph, {}, {})), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        await handler.handle_question_response(
            {"thread_id": "t_question", "agent_name": "rails_plan_mode_agent", "answer": answer},
            ws,
        )


@pytest.mark.asyncio
async def test_a_second_answer_to_a_finished_turn_does_not_resume_the_graph():
    """The bug: the stale answer re-entered the graph and hit the provider empty."""
    graph = _build_graph()
    config = {"configurable": {"thread_id": "t_question"}}
    await graph.ainvoke({"messages": [HumanMessage(content="build me a thing")]}, config)

    handler = RequestHandler(MagicMock())
    await _answer(handler, graph, _connected_websocket(), "Option A")

    # The turn is now finished: no task is paused on an interrupt.
    snap = await graph.aget_state(config)
    assert not any(t.interrupts for t in snap.tasks)
    before = len(snap.values["messages"])

    ws = _connected_websocket()
    await _answer(handler, graph, ws, "Option B")

    after = await graph.aget_state(config)
    assert len(after.values["messages"]) == before, (
        "the stale answer re-entered the graph; that re-entry is what reached the "
        f"provider with an empty history — state is {after.values['messages']!r}"
    )
    assert "system_message" in _sent_types(ws), (
        f"nothing told the user the question had expired; frames sent: {_sent_types(ws)}"
    )
    assert "error" not in _sent_types(ws), "the expired card must not read as a crash"
    assert ws.send_json.call_args_list[-1].args[0]["content"] == QUESTION_ALREADY_ANSWERED_MESSAGE


@pytest.mark.asyncio
async def test_the_first_answer_still_resumes_normally():
    """The guard must not cost a real paused thread its resume."""
    graph = _build_graph()
    config = {"configurable": {"thread_id": "t_question"}}
    await graph.ainvoke({"messages": [HumanMessage(content="build me a thing")]}, config)
    assert any(t.interrupts for t in (await graph.aget_state(config)).tasks)

    await _answer(RequestHandler(MagicMock()), graph, _connected_websocket(), "Option A")

    contents = [getattr(m, "content", "") for m in (await graph.aget_state(config)).values["messages"]]
    assert "User answered: Option A" in contents, (
        f"the guard swallowed a legitimate resume — state is {contents!r}"
    )


class _UnreadableApp:
    """A thread whose state cannot be read right now (checkpointer blip)."""

    def __init__(self):
        self.reads = 0

    async def aget_state(self, config):
        self.reads += 1
        raise RuntimeError("connection is closed")


@pytest.mark.asyncio
async def test_an_unreadable_thread_is_still_resumed():
    """A failed read is not evidence the thread moved on — behave as before."""
    app = _UnreadableApp()
    handler = RequestHandler.__new__(RequestHandler)
    assert await handler._thread_is_still_paused(app, {"configurable": {"thread_id": "t"}}) is True
    assert app.reads == 1
