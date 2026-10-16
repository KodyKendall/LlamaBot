"""A failed sign-in must actually close the socket.

The handler used to send `auth_error` and `break` out of its loop. Returning
from the endpoint does not close an accepted socket under uvicorn, so the
browser never got `onclose`, never reconnected, and the chat showed
"thinking" until a manual reload (crm-4, 2026-10-04 — 12 minutes).

Closing with 4401 also tells the client "get a new token", not "the server
went away".

Run with: pytest app/tests/test_websocket_auth_failure_closes.py -v
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect


@pytest.fixture
def handler():
    from app.websocket.web_socket_handler import WebSocketHandler

    manager = MagicMock()
    manager.app = MagicMock()
    manager.send_personal_message = AsyncMock()
    manager.connect = AsyncMock()
    manager.disconnect = MagicMock()

    websocket = MagicMock()
    websocket.client = "test-client"
    websocket.close = AsyncMock()

    with patch("app.websocket.request_handler.RequestHandler"):
        h = WebSocketHandler(websocket, manager)

    h._is_websocket_open = lambda ws: True
    h.request_handler.start_chat_run = AsyncMock()
    h.request_handler.cleanup_connection = MagicMock()
    h.request_handler._run_manager = MagicMock(return_value=MagicMock())
    return h


async def _drive(handler, frames):
    handler.websocket.receive_json = AsyncMock(
        side_effect=[*frames, WebSocketDisconnect(code=1000, reason="done")]
    )
    with patch("app.websocket.web_socket_handler.WS_AUTH_REQUIRED", True), \
         patch("app.permissions.custom_modes", return_value={}):
        await handler.handle_websocket()


def _sent_types(handler):
    return [c.args[0].get("type") for c in handler.manager.send_personal_message.await_args_list
            if isinstance(c.args[0], dict)]


@pytest.mark.asyncio
async def test_bad_auth_frame_closes_with_4401(handler):
    await _drive(handler, [{"type": "auth", "token": "signed-with-the-wrong-key"}])

    assert "auth_error" in _sent_types(handler)
    handler.websocket.close.assert_awaited_once()
    assert handler.websocket.close.await_args.kwargs.get("code") == 4401


@pytest.mark.asyncio
async def test_unauthenticated_chat_frame_closes_with_4401(handler):
    await _drive(handler, [{"message": "hello", "thread_id": "t1"}])

    assert "auth_error" in _sent_types(handler)
    handler.websocket.close.assert_awaited_once()
    assert handler.websocket.close.await_args.kwargs.get("code") == 4401
    handler.request_handler.start_chat_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_disconnect_does_not_close_again(handler):
    """A socket the browser already closed is not ours to close."""
    await _drive(handler, [])
    handler.websocket.close.assert_not_awaited()
