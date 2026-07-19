"""Server-side enforcement of agent modes on the WebSocket.

The bug these lock down: `agent_name` came straight off the wire
(request_handler.get_langgraph_app_and_state) and `self.auth_user` — which
carries the role claim — was set and never read. The only thing stopping a
`user`-role account from running the engineer agent was the dropdown filter in
chat.html, i.e. one edited WebSocket frame.

The gate lives in WebSocketHandler._authorize_agent_mode and runs BEFORE the
type dispatch, so every frame naming a mode is covered — not just chat.

Run with: pytest app/tests/test_websocket_mode_enforcement.py -v
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.permissions import ROLE_MODES_SETTING_KEY


@pytest.fixture
def engine():
    """In-memory DB standing in for the auth DB the gate reads grants from."""
    import app.models  # noqa: F401

    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def handler(engine):
    """A WebSocketHandler wired to the in-memory DB, with the socket mocked out."""
    from app.websocket.web_socket_handler import WebSocketHandler

    manager = MagicMock()
    manager.app = MagicMock()
    manager.send_personal_message = AsyncMock()

    websocket = MagicMock()
    websocket.client = "test-client"

    with patch("app.websocket.request_handler.RequestHandler"):
        h = WebSocketHandler(websocket, manager)

    # The gate opens its own Session(engine); point that at the fixture DB.
    h._test_engine = engine
    h._is_websocket_open = lambda ws: True
    return h


def _as_browser_user(handler, role="user", is_admin=False):
    """Authenticate the handler as a browser client (the only enforced path)."""
    handler.authenticated = True
    handler.auth_user = {
        "sub": "alice",
        "user_id": 1,
        "role": role,
        "is_admin": is_admin,
        "type": "ws_auth",
    }


def _configure(engine, mapping):
    from app.models import SiteSetting

    with Session(engine) as s:
        existing = s.get(SiteSetting, ROLE_MODES_SETTING_KEY)
        if existing:
            existing.value = json.dumps(mapping)
        else:
            s.add(SiteSetting(key=ROLE_MODES_SETTING_KEY, value=json.dumps(mapping)))
        s.commit()


async def _authorize(handler, frame):
    """Run the gate with app.db.engine pointed at the test DB."""
    with patch("app.db.engine", handler._test_engine), \
         patch("app.permissions.custom_modes", return_value={}):
        return await handler._authorize_agent_mode(frame)


# --- the core regression --------------------------------------------------

@pytest.mark.asyncio
async def test_user_role_cannot_run_engineer_agent(handler):
    """THE bug: a hand-edited frame naming a graph the role was never granted."""
    _as_browser_user(handler, role="user")
    assert await _authorize(handler, {"message": "hi", "agent_name": "rails_agent"}) is False


@pytest.mark.asyncio
async def test_denial_tells_the_client_why(handler):
    _as_browser_user(handler, role="user")
    await _authorize(handler, {"agent_name": "pyxl_agent"})
    handler.manager.send_personal_message.assert_awaited_once()
    payload = handler.manager.send_personal_message.await_args[0][0]
    assert payload["type"] == "error"
    assert "access" in payload["content"]


@pytest.mark.asyncio
async def test_user_role_can_run_its_granted_mode(handler):
    _as_browser_user(handler, role="user")
    assert await _authorize(handler, {"message": "hi", "agent_name": "rails_plain_chat_mode"}) is True


@pytest.mark.asyncio
async def test_engineer_role_can_run_engineer_agent(handler):
    _as_browser_user(handler, role="engineer")
    assert await _authorize(handler, {"message": "hi", "agent_name": "rails_agent"}) is True


# --- the gate covers every frame type, not just chat ----------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("frame_type", ["approval_response", "question_response"])
async def test_resume_frames_are_gated_too(handler, frame_type):
    """These dispatch to get_langgraph_app_and_state with agent_name as well."""
    _as_browser_user(handler, role="user")
    frame = {"type": frame_type, "thread_id": "t1", "agent_name": "rails_agent"}
    assert await _authorize(handler, frame) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"type": "ping"},
    {"type": "cancel", "thread_id": "t1"},
    {"type": "attach", "thread_id": "t1", "last_seq": 3},
    {"message": "no agent_name here"},
])
async def test_frames_without_an_agent_name_pass_through(handler, frame):
    _as_browser_user(handler, role="user")
    assert await _authorize(handler, frame) is True


# --- admin superset --------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_can_run_any_mode_regardless_of_role(handler):
    _as_browser_user(handler, role="user", is_admin=True)
    assert await _authorize(handler, {"agent_name": "rails_agent"}) is True


# --- admin config takes effect on the socket, not just the dropdown -------

@pytest.mark.asyncio
async def test_admin_granting_a_mode_to_a_role_takes_effect(handler, engine):
    _as_browser_user(handler, role="user")
    assert await _authorize(handler, {"agent_name": "rails_beginner_agent"}) is False

    _configure(engine, {"user": ["chat", "beginner"]})
    assert await _authorize(handler, {"agent_name": "rails_beginner_agent"}) is True


@pytest.mark.asyncio
async def test_admin_revoking_a_mode_from_a_role_takes_effect(handler, engine):
    _as_browser_user(handler, role="engineer")
    assert await _authorize(handler, {"agent_name": "pyxl_agent"}) is True

    _configure(engine, {"engineer": ["ticket", "engineer"]})
    assert await _authorize(handler, {"agent_name": "pyxl_agent"}) is False


# --- non-browser callers keep their existing behaviour --------------------

@pytest.mark.asyncio
async def test_rails_gem_token_is_not_gated(handler):
    """verify_rails_token returns no role claim; the gem is a trusted internal caller.

    NOTE: that trust is currently unverified (token_service.verify_rails_token does
    no signature check) — tracked separately. This test pins the gate's behaviour,
    not an endorsement of that path.
    """
    handler.authenticated = True
    handler.auth_user = {"sub": "rails_gem", "type": "rails_auth", "source": "llama_bot_rails"}
    assert await _authorize(handler, {"agent_name": "rails_agent"}) is True


@pytest.mark.asyncio
async def test_unauthenticated_is_left_to_ws_auth_required(handler):
    """WS_AUTH_REQUIRED governs this path; the gate has no role to check."""
    handler.authenticated = False
    handler.auth_user = None
    assert await _authorize(handler, {"agent_name": "rails_agent"}) is True


# --- the wiring ------------------------------------------------------------
# The tests above call the gate directly, so they'd all still pass if the call
# site in handle_websocket vanished. These drive the real dispatch loop.


async def _drive(handler, frames):
    """Feed frames through handle_websocket, then disconnect to end the loop."""
    from fastapi import WebSocketDisconnect

    handler.websocket.receive_json = AsyncMock(
        side_effect=[*frames, WebSocketDisconnect(code=1000, reason="done")]
    )
    handler.manager.connect = AsyncMock()
    handler.manager.disconnect = MagicMock()
    handler.manager.deduplicator.register = MagicMock(return_value=True)
    handler.request_handler.start_chat_run = AsyncMock()
    handler.request_handler.cleanup_connection = MagicMock()
    handler.request_handler._run_manager = MagicMock()

    with patch("app.db.engine", handler._test_engine), \
         patch("app.permissions.custom_modes", return_value={}):
        await handler.handle_websocket()


@pytest.mark.asyncio
async def test_dispatch_loop_blocks_an_ungranted_chat_frame(handler):
    """End-to-end through handle_websocket: the run must never start."""
    _as_browser_user(handler, role="user")
    await _drive(handler, [{"message": "hi", "agent_name": "rails_agent", "thread_id": "t1"}])
    handler.request_handler.start_chat_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_loop_allows_a_granted_chat_frame(handler):
    """The mirror: a granted mode still reaches the agent."""
    _as_browser_user(handler, role="user")
    await _drive(handler, [{"message": "hi", "agent_name": "rails_plain_chat_mode", "thread_id": "t1"}])
    handler.request_handler.start_chat_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_loop_blocks_an_ungranted_approval_frame(handler):
    _as_browser_user(handler, role="user")
    handler.request_handler.start_resume_run = AsyncMock()
    await _drive(handler, [
        {"type": "approval_response", "thread_id": "t1", "agent_name": "rails_agent"},
    ])
    handler.request_handler.start_resume_run.assert_not_awaited()


# --- failure mode ----------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_fails_closed_when_the_grant_cannot_be_read(handler):
    """A DB blip must not become an open door."""
    _as_browser_user(handler, role="user")
    with patch("app.permissions.can_run_agent", side_effect=RuntimeError("db down")), \
         patch("app.permissions.custom_modes", return_value={}):
        assert await handler._authorize_agent_mode({"agent_name": "rails_agent"}) is False
