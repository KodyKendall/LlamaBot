"""A chat whose saved messages are gone must not open as a blank new chat (0.7.14).

A mothership backup truncated the LangGraph checkpoint tables on live boxes while
leaving `threadmetadata` alone, so the sidebar listed hundreds of chats that each
opened as "Hi! What are we building today?" with no error. `/chat-history` now
marks that case so the UI can say what happened.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langgraph.types import StateSnapshot
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import ThreadMetadata
from app.routers import api


def _snapshot(messages):
    return StateSnapshot(
        values={"messages": messages} if messages is not None else {},
        next=(),
        config={"configurable": {"thread_id": "t1"}},
        metadata=None,
        created_at=None,
        parent_config=None,
        tasks=(),
        interrupts=(),
    )


class _FakeGraph:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def aget_state(self, config):
        return self.snapshot


def _request(snapshot):
    state = SimpleNamespace(
        compiled_graphs={"llamabot": _FakeGraph(snapshot)},
        get_or_create_checkpointer=lambda: None,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng, tables=[ThreadMetadata.__table__])
    with patch("app.db.engine", eng):
        yield eng


def _add_thread(engine, thread_id, message_count, title="Quarterly analysis"):
    with Session(engine) as session:
        session.add(ThreadMetadata(thread_id=thread_id, title=title, message_count=message_count))
        session.commit()


@pytest.mark.asyncio
async def test_listed_thread_with_no_checkpoint_is_marked_missing(engine):
    _add_thread(engine, "t1", message_count=12)

    result = await api.chat_history("t1", _request(_snapshot(None)), username="u")

    values = result[0]
    assert values["history_missing"] is True
    assert values["messages"] == []
    assert values["title"] == "Quarterly analysis"
    assert values["message_count"] == 12


@pytest.mark.asyncio
async def test_brand_new_thread_is_not_marked_missing(engine):
    _add_thread(engine, "t2", message_count=0)

    result = await api.chat_history("t2", _request(_snapshot(None)), username="u")

    assert not result[0].get("history_missing")


@pytest.mark.asyncio
async def test_unknown_thread_is_not_marked_missing(engine):
    result = await api.chat_history("nope", _request(_snapshot(None)), username="u")

    assert not result[0].get("history_missing")


@pytest.mark.asyncio
async def test_normal_thread_keeps_its_shape(engine):
    _add_thread(engine, "t3", message_count=2)
    snapshot = _snapshot([{"type": "human", "content": "hi"}])

    result = await api.chat_history("t3", _request(snapshot), username="u")

    assert result[0] == {"messages": [{"type": "human", "content": "hi"}]}
    assert "history_missing" not in result[0]


def test_stray_print_is_gone():
    import inspect

    assert "print(" not in inspect.getsource(api.chat_history)
