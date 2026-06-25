"""
Tests for the ask_user_question / ask_user_uiux_question bypass guard.

When the graph is paused on a question-type interrupt and the user types into the
main chat box instead of the inline control, the handler must route that text in as
the tool answer (resume) rather than appending a HumanMessage ahead of the unanswered
tool call — which violates the tool-call protocol and 400s the next model call
(SupportIncident #106).

_pending_question_interrupt() is the detection used to decide that routing.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _snapshot_with_interrupt(value):
    intr = MagicMock()
    intr.value = value
    task = MagicMock()
    task.interrupts = [intr]
    snap = MagicMock()
    snap.tasks = [task]
    return snap


def _handler():
    from app.websocket.request_handler import RequestHandler
    return RequestHandler.__new__(RequestHandler)


class TestPendingQuestionInterrupt:
    @pytest.mark.asyncio
    async def test_detects_uiux_question(self):
        h = _handler()
        app = AsyncMock()
        app.aget_state = AsyncMock(return_value=_snapshot_with_interrupt(
            {"type": "uiux_question", "question": "Which layout?", "options": []}
        ))
        result = await h._pending_question_interrupt(app, {})
        assert result is not None
        assert result["type"] == "uiux_question"

    @pytest.mark.asyncio
    async def test_detects_user_question(self):
        h = _handler()
        app = AsyncMock()
        app.aget_state = AsyncMock(return_value=_snapshot_with_interrupt(
            {"type": "user_question", "question": "What color?", "options": []}
        ))
        result = await h._pending_question_interrupt(app, {})
        assert result is not None
        assert result["type"] == "user_question"

    @pytest.mark.asyncio
    async def test_ignores_hitl_approval_interrupt(self):
        """Approval interrupts expect structured decisions, not free text — don't reroute."""
        h = _handler()
        app = AsyncMock()
        app.aget_state = AsyncMock(return_value=_snapshot_with_interrupt(
            {"action_requests": [{"name": "bash_command", "args": {}}]}
        ))
        result = await h._pending_question_interrupt(app, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_none_when_no_interrupt(self):
        h = _handler()
        snap = MagicMock()
        snap.tasks = []
        app = AsyncMock()
        app.aget_state = AsyncMock(return_value=snap)
        result = await h._pending_question_interrupt(app, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_none_and_swallows_state_error(self):
        """A failure reading state must not crash the request path — fall through to normal."""
        h = _handler()
        app = AsyncMock()
        app.aget_state = AsyncMock(side_effect=RuntimeError("db down"))
        result = await h._pending_question_interrupt(app, {})
        assert result is None
