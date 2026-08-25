"""Control frames must not be reachable before authentication.

`cancel`, `attach`, `approval_response` and `question_response` were dispatched
*above* the auth check in `handle_websocket`, so they bypassed WS_AUTH_REQUIRED
even when it was switched on. Two of them have real reach:

  * `attach` replays a background run's buffered output by `thread_id` — an
    anonymous socket could read another connection's conversation.
  * `approval_response` resumes a run parked on a human approval, i.e. it can
    approve a tool call somebody else was asked to confirm.

Only `ping` and `auth` are legitimately pre-auth frames.

Run with: pytest app/tests/test_websocket_preauth_frames.py -v
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
    manager.deduplicator.register = MagicMock(return_value=True)

    websocket = MagicMock()
    websocket.client = "test-client"

    with patch("app.websocket.request_handler.RequestHandler"):
        h = WebSocketHandler(websocket, manager)

    h._is_websocket_open = lambda ws: True
    h.request_handler.start_chat_run = AsyncMock()
    h.request_handler.start_resume_run = AsyncMock()
    h.request_handler.cleanup_connection = MagicMock()

    run_manager = MagicMock()
    run_manager.cancel = AsyncMock(return_value=True)
    run_manager.attach = MagicMock(return_value=None)
    h.request_handler._run_manager = MagicMock(return_value=run_manager)
    h._run_manager = run_manager
    return h


async def _drive(handler, frames, auth_required=True):
    handler.websocket.receive_json = AsyncMock(
        side_effect=[*frames, WebSocketDisconnect(code=1000, reason="done")]
    )
    with patch("app.websocket.web_socket_handler.WS_AUTH_REQUIRED", auth_required), \
         patch("app.permissions.custom_modes", return_value={}):
        await handler.handle_websocket()


def _authenticated(handler):
    handler.authenticated = True
    handler.auth_user = {
        "sub": "alice", "user_id": 1, "role": "engineer",
        "is_admin": False, "type": "ws_auth",
    }


class TestUnauthenticatedControlFramesAreRefused:

    @pytest.mark.asyncio
    async def test_attach_does_not_replay_a_run(self, handler):
        await _drive(handler, [{"type": "attach", "thread_id": "someone-elses", "last_seq": 0}])
        handler._run_manager.attach.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_does_not_stop_a_run(self, handler):
        await _drive(handler, [{"type": "cancel", "thread_id": "someone-elses"}])
        handler._run_manager.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approval_response_does_not_resume_a_run(self, handler):
        await _drive(handler, [{"type": "approval_response", "thread_id": "t1", "approved": True}])
        handler.request_handler.start_resume_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_question_response_does_not_resume_a_run(self, handler):
        await _drive(handler, [{"type": "question_response", "thread_id": "t1", "answer": "yes"}])
        handler.request_handler.start_resume_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_client_is_told_it_needs_to_authenticate(self, handler):
        await _drive(handler, [{"type": "attach", "thread_id": "t1"}])
        sent = [c[0][0] for c in handler.manager.send_personal_message.await_args_list]
        assert any(m.get("type") == "auth_error" for m in sent)


class TestLegitimateTrafficIsUnaffected:

    @pytest.mark.asyncio
    async def test_ping_still_answers_before_authentication(self, handler):
        await _drive(handler, [{"type": "ping"}])
        sent = [c[0][0] for c in handler.manager.send_personal_message.await_args_list]
        assert any(m.get("type") == "pong" for m in sent)

    @pytest.mark.asyncio
    async def test_authenticated_attach_still_works(self, handler):
        _authenticated(handler)
        await _drive(handler, [{"type": "attach", "thread_id": "t1", "last_seq": 2}])
        handler._run_manager.attach.assert_called_once()

    @pytest.mark.asyncio
    async def test_authenticated_cancel_still_works(self, handler):
        _authenticated(handler)
        await _drive(handler, [{"type": "cancel", "thread_id": "t1"}])
        handler._run_manager.cancel.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_control_frame_race_does_not_close_the_socket(self, handler):
        """An `attach` can race the async auth handshake on reconnect.

        Dropping the frame is right; killing the connection would turn a race
        into a reconnect loop, so the socket must survive to serve the
        `auth` frame that is right behind it.
        """
        await _drive(handler, [
            {"type": "attach", "thread_id": "t1"},
            {"type": "ping"},
        ])
        sent = [c[0][0] for c in handler.manager.send_personal_message.await_args_list]
        assert any(m.get("type") == "pong" for m in sent), "socket closed on a raced attach"

    @pytest.mark.asyncio
    async def test_boxes_with_auth_switched_off_keep_working(self, handler):
        await _drive(handler, [{"type": "attach", "thread_id": "t1"}], auth_required=False)
        handler._run_manager.attach.assert_called_once()
