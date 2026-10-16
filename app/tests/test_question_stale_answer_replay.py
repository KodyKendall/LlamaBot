"""
Regression: a question card was resumed with a STALE answer, forever.

Reported by Mother Leo on leo-lanupa (thread b560739f, rails_plan_mode_agent,
LlamaBot 0.7.12). One ask_user_question card got three ToolMessages, all
"User answered: Sao rồi bạn?" — the third in reply to a completely different
message the user typed four minutes later. The model kept seeing "how's it
going?" and answered with status reports instead of working.

How it happens. When ``interrupt()`` takes an answer, LangGraph saves that
answer against the paused task straight away. If the run is then cancelled
before the tool step is saved (RunManager cancels the running turn whenever a
new message arrives), the thread is left paused on the SAME card with the old
answer still attached. Every later resume, whatever the user sends, re-runs
the tool and ``interrupt()`` hands back the old answer. A resume keyed by
interrupt id does not help: LangGraph appends it after the saved one.

The fix replaces the saved answer with the new one before resuming, on both
paths that answer a question card: the card itself (handle_question_response)
and the main chat box (handle_request).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from starlette.websockets import WebSocketState

from app.agents.leonardo.rails_plan_mode_agent.nodes import ask_user_question
from app.websocket.request_handler import RequestHandler


class _Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _SlowAfterTool(AgentMiddleware):
    """Holds the tool step open after the tool returns, so a cancel can land
    after ``interrupt()`` took the answer but before the step is saved."""

    def __init__(self):
        super().__init__()
        self.slow = False

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        if self.slow:
            await asyncio.sleep(5)
        return result


def _build_agent():
    slow = _SlowAfterTool()
    agent = create_agent(
        _Model(responses=[
            AIMessage(content="", id="ai_ask", tool_calls=[{
                "name": "ask_user_question", "id": "call_q",
                "args": {"questions": [{"question": "Which layout?"}]},
            }]),
            AIMessage(content="on it", id="ai_after_answer"),
            AIMessage(content="reply to the typed message", id="ai_after_chat"),
        ]),
        tools=[ask_user_question],
        middleware=[slow],
        checkpointer=MemorySaver(),
    )
    return agent, slow


def _connected_websocket():
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


def _handler(agent, typed_message=None):
    handler = RequestHandler(MagicMock())
    handler.app.state.mothership_client = None
    state = {"messages": [HumanMessage(content=typed_message or "")]}
    patches = [
        patch.object(handler, "get_langgraph_app_and_state", return_value=(agent, state, {})),
        patch.object(handler, "_report_error_to_mothership", AsyncMock()),
        patch.object(handler, "_check_instance_lock_or_block", AsyncMock(return_value=False)),
        patch.object(handler, "_paywall_blocks_turn", AsyncMock(return_value=False)),
        patch.object(handler, "_warn_if_model_substituted", AsyncMock()),
    ]
    return handler, patches


async def _pause_on_question(agent, config):
    await agent.ainvoke({"messages": [HumanMessage(content="plan me an app")]}, config)
    snap = await agent.aget_state(config)
    assert any(t.interrupts for t in snap.tasks), "agent did not pause on the question"


async def _answer_then_cancel(agent, slow, config, answer):
    """The incident: the answer is taken, then the run is cancelled mid-step.

    Streamed like the handler streams it — a cancelled ``astream`` saves the
    taken answer against the task (a cancelled ``ainvoke`` does not).
    """
    async def stream():
        async for _ in agent.astream(Command(resume=answer), config,
                                     stream_mode=["updates"], subgraphs=True):
            pass

    slow.slow = True
    run = asyncio.create_task(stream())
    await asyncio.sleep(0.3)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    slow.slow = False
    snap = await agent.aget_state(config)
    assert any(t.interrupts for t in snap.tasks), (
        "precondition: after the cancel the thread is still paused on the same card"
    )


def _tool_messages(snap, tool_call_id="call_q"):
    return [m for m in snap.values["messages"]
            if isinstance(m, ToolMessage) and m.tool_call_id == tool_call_id]


@pytest.mark.asyncio
async def test_card_answer_after_a_cancelled_answer_is_not_replaced_by_the_old_one():
    agent, slow = _build_agent()
    config = {"configurable": {"thread_id": "t_card"}}
    await _pause_on_question(agent, config)
    await _answer_then_cancel(agent, slow, config, "Sao rồi bạn?")

    handler, patches = _handler(agent)
    with patches[0], patches[1]:
        await handler.handle_question_response(
            {"thread_id": "t_card", "agent_name": "rails_plan_mode_agent",
             "answer": "Two columns"},
            _connected_websocket(),
        )

    answers = [m.content for m in _tool_messages(await agent.aget_state(config))]
    assert answers == ["User answered: Two columns"], (
        f"the card was answered with a stale value: {answers!r}"
    )


@pytest.mark.asyncio
async def test_typed_message_after_a_cancelled_answer_is_not_replaced_by_the_old_one():
    agent, slow = _build_agent()
    config = {"configurable": {"thread_id": "t_chat"}}
    await _pause_on_question(agent, config)
    await _answer_then_cancel(agent, slow, config, "Sao rồi bạn?")

    typed = "BẠN ĐÃ RÀ SOÁT XONG KẾ HOẠCH CHƯA?"
    handler, patches = _handler(agent, typed_message=typed)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        await handler.handle_request(
            {"thread_id": "t_chat", "agent_name": "rails_plan_mode_agent", "message": typed},
            _connected_websocket(),
        )

    answers = [m.content for m in _tool_messages(await agent.aget_state(config))]
    assert answers == [f"User answered: {typed}"], (
        f"the typed message was swapped for a stale answer: {answers!r}"
    )


@pytest.mark.asyncio
async def test_typed_message_after_a_clean_answer_is_a_new_human_message():
    """Ticket steps 1-3 on a healthy thread: one answer, then normal chat."""
    agent, _ = _build_agent()
    config = {"configurable": {"thread_id": "t_clean"}}
    await _pause_on_question(agent, config)

    handler, patches = _handler(agent)
    with patches[0], patches[1]:
        await handler.handle_question_response(
            {"thread_id": "t_clean", "agent_name": "rails_plan_mode_agent", "answer": "A"},
            _connected_websocket(),
        )

    handler, patches = _handler(agent, typed_message="B")
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        await handler.handle_request(
            {"thread_id": "t_clean", "agent_name": "rails_plan_mode_agent", "message": "B"},
            _connected_websocket(),
        )

    snap = await agent.aget_state(config)
    assert [m.content for m in _tool_messages(snap)] == ["User answered: A"]
    humans = [m.content for m in snap.values["messages"] if isinstance(m, HumanMessage)]
    assert humans[-1] == "B", f"the typed message did not arrive as chat: {humans!r}"
