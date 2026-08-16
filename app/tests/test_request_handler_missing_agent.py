"""A payload with no ``agent_name`` must not crash inside the stream loop.

``get_langgraph_app_and_state`` initialises ``app = None`` and only assigns it
inside ``if message.get("agent_name") is not None:``. A frame that omits the key
fell straight through to the stream call:

    File "app/websocket/request_handler.py", line 848, in handle_request
      async for chunk in app.astream(stream_input, config=config, …)
    AttributeError: 'NoneType' object has no attribute 'astream'

An *unknown* agent_name already raises a clear ``ValueError``; the missing key
did not. 6 occurrences on 0.7.0 (internal boxes only, via the WebSocket
integration harness) — no customer impact, but the path is real and the browser
gets a stack trace instead of a message.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState

from app.websocket.request_handler import RequestHandler


def _connected_websocket():
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


def _handler():
    handler = RequestHandler(MagicMock())
    handler.app.state.mothership_client = None
    return handler


@pytest.mark.asyncio
async def test_missing_agent_name_reports_an_error_instead_of_crashing():
    handler = _handler()
    websocket = _connected_websocket()

    with patch.object(handler, "_check_instance_lock_or_block", AsyncMock(return_value=False)), \
         patch.object(handler, "_check_paywall_or_block", AsyncMock(return_value=False)), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        # No agent_name key at all — the shape the integration harness sends.
        await handler.handle_request(
            {"thread_id": "ws_integration_thread", "message": "hello"}, websocket
        )

    frames = [c.args[0] for c in websocket.send_json.await_args_list]
    errors = [f for f in frames if f.get("type") == "error"]
    assert errors, f"the browser was told nothing; frames: {frames}"
    content = errors[-1]["content"]
    assert "agent" in content.lower(), (
        f"the error must name the missing agent, not leak a traceback: {content!r}"
    )
    assert "NoneType" not in content and "astream" not in content, content


@pytest.mark.asyncio
async def test_unknown_agent_name_still_raises_its_named_error():
    """The existing, already-clear failure must not be softened into a no-op.

    (It is a KeyError from the registry lookup, not the ValueError further down
    — the graph entry is missing before the workflow string is ever parsed.)
    """
    handler = _handler()

    with pytest.raises(KeyError, match="not found in langgraph registry"):
        handler.get_langgraph_app_and_state(
            {"agent_name": "no_such_agent_at_all", "message": "hi"}
        )
