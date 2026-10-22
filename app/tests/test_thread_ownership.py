"""Chats are private to the LlamaBot user who started them (0.7.15, LP-6).

Every ThreadMetadata row had user_id = NULL, /threads listed every chat on the
box, and /chat-history opened any thread id for anyone signed in — so on a box
shared by two people, each could read the other's chats.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import WebSocketDisconnect
from langgraph.types import StateSnapshot
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import ThreadMetadata, User
from app.routers import api
from app.services.thread_service import backfill_thread_owners, get_thread_list

RICH, RUAN, ADMIN = 1, 2, 3


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng, tables=[User.__table__, ThreadMetadata.__table__])
    with patch("app.db.engine", eng):
        yield eng


def _users(engine, *specs):
    with Session(engine) as session:
        for user_id, name, is_admin in specs:
            session.add(User(id=user_id, username=name, password_hash="x", is_admin=is_admin))
        session.commit()


def _thread(engine, thread_id, user_id, minutes_ago=0, message_count=2):
    with Session(engine) as session:
        session.add(ThreadMetadata(
            thread_id=thread_id, title=thread_id, user_id=user_id, message_count=message_count,
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        ))
        session.commit()


@pytest.fixture
def two_users(engine):
    _users(engine, (RICH, "rich", False), (RUAN, "ruan", False), (ADMIN, "admin", True))
    _thread(engine, "rich-1", RICH, minutes_ago=1)
    _thread(engine, "ruan-1", RUAN, minutes_ago=2)
    _thread(engine, "legacy-1", None, minutes_ago=3)
    return engine


# ---------------------------------------------------------------- recording the owner

@pytest.mark.asyncio
async def test_new_chat_metadata_records_the_signed_in_user(engine):
    from app.lib.request_context import reset_current_user_id, set_current_user_id
    from app.websocket.request_handler import RequestHandler

    handler = RequestHandler.__new__(RequestHandler)
    token = set_current_user_id(RUAN)
    try:
        with patch("app.services.thread_service.schedule_title_generation", new=AsyncMock()):
            await handler._update_thread_metadata(
                {"thread_id": "t-new", "message": "hello", "agent_name": "rails_agent",
                 "user_id": RICH}  # a browser-sent value must be ignored
            )
    finally:
        reset_current_user_id(token)

    with Session(engine) as session:
        assert session.get(ThreadMetadata, "t-new").user_id == RUAN


# ---------------------------------------------------------------- listing

def _titles(rows):
    return [r.thread_id for r in rows]


def test_thread_list_filters_by_owner(two_users):
    with Session(two_users) as session:
        assert _titles(get_thread_list(session, user_id=RUAN)) == ["ruan-1"]
        assert _titles(get_thread_list(session, user_id=ADMIN, include_unowned=True)) == ["legacy-1"]


async def _threads_as(username, before=None, limit=10):
    return await api.threads(SimpleNamespace(), username=username, before=before, limit=limit)


@pytest.mark.asyncio
async def test_threads_endpoint_lists_only_own_chats(two_users):
    rich = await _threads_as("rich")
    ruan = await _threads_as("ruan")

    assert [t["thread_id"] for t in rich["threads"]] == ["rich-1"]
    assert [t["thread_id"] for t in ruan["threads"]] == ["ruan-1"]


@pytest.mark.asyncio
async def test_unowned_legacy_chats_show_only_to_admins(two_users):
    admin = await _threads_as("admin")
    assert [t["thread_id"] for t in admin["threads"]] == ["legacy-1"]


@pytest.mark.asyncio
async def test_load_more_cursor_stays_private(engine):
    _users(engine, (RICH, "rich", False), (RUAN, "ruan", False))
    for i in range(7):  # interleave so a leak would land on every page
        _thread(engine, f"rich-{i}", RICH, minutes_ago=2 * i)
        _thread(engine, f"ruan-{i}", RUAN, minutes_ago=2 * i + 1)

    seen = []
    page = await _threads_as("ruan", limit=5)
    seen += page["threads"]
    assert page["has_more"] is True
    page = await _threads_as("ruan", before=page["next_cursor"], limit=5)
    seen += page["threads"]

    assert sorted(t["thread_id"] for t in seen) == sorted(f"ruan-{i}" for i in range(7))
    assert page["has_more"] is False


# ---------------------------------------------------------------- opening by id

def _snapshot(thread_id, messages):
    return StateSnapshot(
        values={"messages": messages} if messages else {},
        next=(), config={"configurable": {"thread_id": thread_id}}, metadata=None,
        created_at=None, parent_config=None, tasks=(), interrupts=(),
    )


class _Graph:
    """Checkpointer stand-in: only rich-1 has saved messages."""

    async def aget_state(self, config):
        tid = config["configurable"]["thread_id"]
        messages = [{"type": "human", "content": "Private note from Rich"}] if tid == "rich-1" else None
        return _snapshot(tid, messages)


def _request():
    state = SimpleNamespace(compiled_graphs={"llamabot": _Graph(), "rails_agent": _Graph()},
                            get_or_create_checkpointer=lambda: None)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _as_missing(result, thread_id):
    """The response with the requested id swapped out, so two ids can be compared."""
    return repr(result).replace(thread_id, "<id>")


@pytest.mark.asyncio
async def test_owner_can_open_their_chat(two_users):
    result = await api.chat_history("rich-1", _request(), username="rich")
    assert result[0]["messages"]


@pytest.mark.asyncio
async def test_other_users_chat_answers_exactly_like_a_missing_one(two_users):
    foreign = await api.chat_history("rich-1", _request(), username="ruan")
    missing = await api.chat_history("no-such-thread", _request(), username="ruan")

    assert "Private note" not in repr(foreign)
    assert _as_missing(foreign, "rich-1") == _as_missing(missing, "no-such-thread")


@pytest.mark.asyncio
async def test_other_users_lost_history_is_not_announced(two_users):
    """The 0.7.14 history_missing marker would confirm the chat exists."""
    with Session(two_users) as session:
        session.add(ThreadMetadata(thread_id="rich-lost", title="Budget", user_id=RICH, message_count=9))
        session.commit()

    foreign = await api.chat_history("rich-lost", _request(), username="ruan")
    assert not foreign[0].get("history_missing")
    assert "Budget" not in repr(foreign)


@pytest.mark.asyncio
async def test_other_users_token_count_reads_as_empty(two_users):
    result = await api.thread_token_count("rich-1", _request(), agent_name="rails_agent", username="ruan")
    assert result == {"token_count": 0, "message_count": 0}


@pytest.mark.asyncio
async def test_other_users_chat_cannot_be_compacted(two_users):
    from fastapi import HTTPException

    body = api.CompactThreadRequest(thread_id="rich-1")
    with pytest.raises(HTTPException) as err:
        await api.compact_thread(body, _request(), username="ruan")
    assert err.value.status_code == 404


# ---------------------------------------------------------------- websocket

@pytest.fixture
def ws(two_users):
    from app.websocket.web_socket_handler import WebSocketHandler

    manager = MagicMock()
    manager.send_personal_message = AsyncMock()
    manager.connect = AsyncMock()
    manager.deduplicator.register = MagicMock(return_value=True)
    websocket = MagicMock()
    websocket.close = AsyncMock()

    with patch("app.websocket.request_handler.RequestHandler"):
        h = WebSocketHandler(websocket, manager)
    h._is_websocket_open = lambda _ws: True
    h.authenticated = True
    h.auth_user = {"sub": "ruan", "user_id": RUAN, "role": "engineer", "is_admin": False}
    h.request_handler.start_chat_run = AsyncMock()
    h.request_handler.start_resume_run = AsyncMock()
    run_manager = MagicMock()
    run_manager.cancel = AsyncMock(return_value=True)
    h.request_handler._run_manager = MagicMock(return_value=run_manager)
    return h


async def _drive(h, frames):
    h.websocket.receive_json = AsyncMock(side_effect=[*frames, WebSocketDisconnect(code=1000)])
    with patch("app.websocket.web_socket_handler.WS_AUTH_REQUIRED", True):
        await h.handle_websocket()


def _sent(h):
    return [c.args[0] for c in h.manager.send_personal_message.await_args_list]


@pytest.mark.asyncio
async def test_message_to_another_users_thread_is_refused(ws):
    await _drive(ws, [{"message": "hijack", "thread_id": "rich-1"}])

    ws.request_handler.start_chat_run.assert_not_awaited()
    assert any(f.get("type") == "error" for f in _sent(ws))


@pytest.mark.asyncio
async def test_message_to_own_and_new_threads_runs(ws):
    await _drive(ws, [{"message": "hi", "thread_id": "ruan-1"},
                      {"message": "hi", "thread_id": "brand-new"}])
    assert ws.request_handler.start_chat_run.await_count == 2


@pytest.mark.asyncio
async def test_attach_to_another_users_run_looks_like_no_run(ws):
    await _drive(ws, [{"type": "attach", "thread_id": "rich-1", "last_seq": 0}])

    ws.request_handler._run_manager.return_value.attach.assert_not_called()
    assert _sent(ws) == [{"type": "no_active_run", "thread_id": "rich-1"}]


@pytest.mark.asyncio
async def test_resume_and_cancel_on_another_users_thread_are_refused(ws):
    await _drive(ws, [
        {"type": "approval_response", "thread_id": "rich-1", "approved": True},
        {"type": "question_response", "thread_id": "rich-1", "answer": "x"},
        {"type": "cancel", "thread_id": "rich-1"},
    ])
    ws.request_handler.start_resume_run.assert_not_awaited()
    ws.request_handler._run_manager.return_value.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_rails_gem_socket_is_not_owner_checked(ws):
    """The embedded Rails chat has no LlamaBot account to own anything."""
    ws.auth_user = {"sub": "rails_gem", "rails_user_id": 5, "role": "rails"}
    await _drive(ws, [{"message": "hi", "thread_id": "rich-1"}])
    ws.request_handler.start_chat_run.assert_awaited_once()


# ---------------------------------------------------------------- backfill

def test_backfill_assigns_unowned_chats_on_a_one_user_box(engine):
    _users(engine, (RICH, "rich", False))
    _thread(engine, "old-1", None)
    _thread(engine, "old-2", None)

    with Session(engine) as session:
        assert backfill_thread_owners(session) == 2
        assert {t.user_id for t in session.exec(select(ThreadMetadata))} == {RICH}


def test_backfill_leaves_unowned_chats_on_a_multi_user_box(engine):
    _users(engine, (RICH, "rich", False), (RUAN, "ruan", False))
    _thread(engine, "old-1", None)

    with Session(engine) as session:
        assert backfill_thread_owners(session) == 0
        assert session.get(ThreadMetadata, "old-1").user_id is None


@pytest.mark.asyncio
async def test_one_user_box_keeps_its_history_after_upgrade(engine):
    _users(engine, (RICH, "rich", False))  # not an admin: the backfill is what keeps it
    _thread(engine, "old-1", None)
    with Session(engine) as session:
        backfill_thread_owners(session)

    listed = await _threads_as("rich")
    assert [t["thread_id"] for t in listed["threads"]] == ["old-1"]
