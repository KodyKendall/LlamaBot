from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

from app.websocket.request_handler import RequestHandler


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
