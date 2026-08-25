from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from starlette.websockets import WebSocketState

from app.agents.leonardo.delegation import run_delegation
from app.websocket.request_handler import RequestHandler
from app.websocket.run_manager import RunHandle, RunSink, ThreadOutputLog


@pytest.mark.asyncio
async def test_custom_delegation_progress_is_forwarded_to_websocket():
    handler = RequestHandler(MagicMock())
    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()
    payload = {
        "type": "delegation_progress",
        "phase": "working",
        "elapsed_seconds": 15,
        "message": "Research sub-agent is still working… (15s)",
    }

    handled = await handler._forward_custom_stream_chunk(
        (("tools:delegate_task",), "custom", payload), websocket
    )

    assert handled is True
    websocket.send_json.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_unknown_custom_event_is_not_exposed_to_browser():
    handler = RequestHandler(MagicMock())
    websocket = MagicMock()
    websocket.client_state = WebSocketState.CONNECTED
    websocket.send_json = AsyncMock()

    handled = await handler._forward_custom_stream_chunk(
        ((), "custom", {"type": "internal_secret_event"}), websocket
    )

    assert handled is True
    websocket.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_langgraph_progress_reaches_thread_scoped_websocket():
    """Exercise runtime writer -> LangGraph custom stream -> handler -> RunSink."""
    payload_result = {"messages": []}

    class SuccessfulNestedAgent:
        async def ainvoke(self, _input, config=None):
            return payload_result

        async def astream(self, _input, config=None, stream_mode=None):
            yield payload_result

    async def delegate_node(state):
        await run_delegation(
            SuccessfulNestedAgent(),
            {"messages": []},
            stream_writer=get_stream_writer(),
            heartbeat_seconds=60,
            label="Research sub-agent",
        )
        return state

    builder = StateGraph(dict)
    builder.add_node("delegate", delegate_node)
    builder.add_edge(START, "delegate")
    builder.add_edge("delegate", END)
    graph = builder.compile()

    chunks = [
        chunk
        async for chunk in graph.astream(
            {}, stream_mode=["custom"], subgraphs=True
        )
    ]
    assert [chunk[2]["phase"] for chunk in chunks] == ["started", "completed"]

    live_websocket = MagicMock()
    live_websocket.client_state = WebSocketState.CONNECTED
    live_websocket.send_json = AsyncMock()
    handle = RunHandle("thread-owner", ThreadOutputLog())
    handle.attached_ws = live_websocket
    sink = RunSink(handle)
    handler = RequestHandler(MagicMock())

    for chunk in chunks:
        assert await handler._forward_custom_stream_chunk(chunk, sink) is True

    forwarded = [call.args[0] for call in live_websocket.send_json.await_args_list]
    assert [frame["phase"] for frame in forwarded] == ["started", "completed"]
    assert [frame["thread_id"] for frame in forwarded] == [
        "thread-owner",
        "thread-owner",
    ]
    assert [frame["seq"] for frame in forwarded] == [1, 2]
