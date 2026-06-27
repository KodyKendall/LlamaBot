"""
Tests for the visual UI/UX question flow:
- ask_user_uiux_question tool fires the correct interrupt and returns the user's choice
- the tool is registered in the plan mode agent's default_tools
- _check_and_send_interrupts forwards a uiux_question interrupt to the frontend as
  a uiux_question_request, preserving the structured options (id/label/html)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import ToolMessage
from langgraph.types import Command


SAMPLE_OPTIONS = [
    {"id": "centered", "label": "Centered hero", "html": '<button class="btn btn-primary">Save</button>'},
    {"id": "split", "label": "Split hero", "html": '<div class="card bg-base-200 p-4">Split</div>'},
]


class TestAskUserUiuxQuestionTool:
    """Test the ask_user_uiux_question tool in the plan mode agent."""

    def test_uiux_question_fires_interrupt(self):
        """ask_user_uiux_question should call interrupt() with the uiux_question payload."""
        from app.agents.leonardo.rails_plan_mode_agent.nodes import ask_user_uiux_question

        with patch(
            "app.agents.leonardo.rails_plan_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "centered: Centered hero"

            mock_runtime = MagicMock()
            mock_runtime.tool_call_id = "tool_call_abc"

            result = ask_user_uiux_question.func(
                question="Which hero layout do you prefer?",
                options=SAMPLE_OPTIONS,
                runtime=mock_runtime,
                context="Phase 1: Clarify",
            )

            mock_interrupt.assert_called_once_with({
                "type": "uiux_question",
                "question": "Which hero layout do you prefer?",
                "options": SAMPLE_OPTIONS,
                "context": "Phase 1: Clarify",
            })

            assert isinstance(result, Command)
            messages = result.update["messages"]
            assert len(messages) == 1
            assert isinstance(messages[0], ToolMessage)
            assert "centered: Centered hero" in messages[0].content
            assert messages[0].tool_call_id == "tool_call_abc"

    def test_uiux_question_in_default_tools(self):
        """ask_user_uiux_question should be registered in the plan mode default_tools list."""
        from app.agents.leonardo.rails_plan_mode_agent.nodes import default_tools

        tool_names = [t.name for t in default_tools]
        assert "ask_user_uiux_question" in tool_names


class TestCheckAndSendInterruptsUiuxQuestion:
    """Test that _check_and_send_interrupts forwards uiux_question interrupts."""

    @pytest.mark.asyncio
    async def test_uiux_question_forwarded_with_options(self):
        """A uiux_question interrupt becomes a uiux_question_request preserving options."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "uiux_question",
            "question": "Which hero layout do you prefer?",
            "options": SAMPLE_OPTIONS,
            "context": "Phase 1: Clarify",
        }

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        message_data = {"thread_id": "thread_123", "agent_name": "rails_plan_mode_agent"}

        result = await handler._check_and_send_interrupts(
            mock_app, {}, message_data, mock_websocket
        )

        assert result is True
        mock_websocket.send_json.assert_called_once_with({
            "type": "uiux_question_request",
            "question": "Which hero layout do you prefer?",
            "options": SAMPLE_OPTIONS,
            "context": "Phase 1: Clarify",
            "thread_id": "thread_123",
            "agent_name": "rails_plan_mode_agent",
        })

    @pytest.mark.asyncio
    async def test_uiux_question_defaults_when_fields_missing(self):
        """Missing question/options/context default to empty values, not errors."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        mock_interrupt = MagicMock()
        mock_interrupt.value = {"type": "uiux_question"}

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        message_data = {"thread_id": "t1", "agent_name": "rails_plan_mode_agent"}

        await handler._check_and_send_interrupts(mock_app, {}, message_data, mock_websocket)

        sent = mock_websocket.send_json.call_args[0][0]
        assert sent["type"] == "uiux_question_request"
        assert sent["question"] == ""
        assert sent["options"] == []
        assert sent["context"] == ""


class TestAskUserQuestionUiRelated:
    """Test the ui_related flag on ask_user_question (offers a 'See visual options' choice
    that asks Leo to follow up with ask_user_uiux_question)."""

    def test_ui_related_passed_through_interrupt(self):
        """ask_user_question forwards ui_related=True in the interrupt payload."""
        from app.agents.leonardo.rails_plan_mode_agent.nodes import ask_user_question

        with patch(
            "app.agents.leonardo.rails_plan_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "See visual options"

            mock_runtime = MagicMock()
            mock_runtime.tool_call_id = "tool_call_xyz"

            ask_user_question.func(
                question="How should the hero look?",
                runtime=mock_runtime,
                options=["Big and bold", "Minimal"],
                context="Phase 1: Clarify",
                ui_related=True,
            )

            mock_interrupt.assert_called_once_with({
                "type": "user_question",
                "question": "How should the hero look?",
                "options": ["Big and bold", "Minimal"],
                "context": "Phase 1: Clarify",
                "ui_related": True,
            })

    def test_ui_related_defaults_to_false(self):
        """ui_related defaults to False when the agent omits it."""
        from app.agents.leonardo.rails_plan_mode_agent.nodes import ask_user_question

        with patch(
            "app.agents.leonardo.rails_plan_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "Big and bold"

            mock_runtime = MagicMock()
            mock_runtime.tool_call_id = "tool_call_xyz"

            ask_user_question.func(
                question="What should we name it?",
                runtime=mock_runtime,
            )

            assert mock_interrupt.call_args[0][0]["ui_related"] is False

    @pytest.mark.asyncio
    async def test_ui_related_forwarded_to_frontend(self):
        """A user_question interrupt forwards ui_related in the question_request."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "user_question",
            "question": "How should the hero look?",
            "options": ["Big and bold", "Minimal"],
            "context": "Phase 1: Clarify",
            "ui_related": True,
        }

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        message_data = {"thread_id": "thread_123", "agent_name": "rails_plan_mode_agent"}

        result = await handler._check_and_send_interrupts(
            mock_app, {}, message_data, mock_websocket
        )

        assert result is True
        sent = mock_websocket.send_json.call_args[0][0]
        assert sent["type"] == "question_request"
        assert sent["ui_related"] is True

    @pytest.mark.asyncio
    async def test_ui_related_defaults_false_when_missing(self):
        """A user_question interrupt without ui_related forwards ui_related=False."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        mock_interrupt = MagicMock()
        mock_interrupt.value = {"type": "user_question", "question": "Name?"}

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        message_data = {"thread_id": "t1", "agent_name": "rails_plan_mode_agent"}

        await handler._check_and_send_interrupts(mock_app, {}, message_data, mock_websocket)

        sent = mock_websocket.send_json.call_args[0][0]
        assert sent["type"] == "question_request"
        assert sent["ui_related"] is False
