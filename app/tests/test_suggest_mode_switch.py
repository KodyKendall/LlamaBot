"""
Tests for the suggest-mode-switch flow:
- suggest_plan_mode tool fires the correct interrupt
- _check_and_send_interrupts sends suggest_mode_switch (with original_message) to the frontend
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class TestSuggestPlanModeTool:
    """Test the suggest_plan_mode tool in the beginner agent."""

    def test_suggest_plan_mode_fires_interrupt(self):
        """suggest_plan_mode should call interrupt() with suggest_mode_switch type."""
        from app.agents.leonardo.rails_beginner_agent.nodes import suggest_plan_mode

        with patch(
            "app.agents.leonardo.rails_beginner_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "yes, switch to plan mode"

            mock_runtime = MagicMock()
            mock_runtime.tool_call_id = "tool_call_abc"

            result = suggest_plan_mode.func(
                reason="This request has multiple steps that would benefit from planning first.",
                runtime=mock_runtime,
            )

            mock_interrupt.assert_called_once_with({
                "type": "suggest_mode_switch",
                "target_mode": "plan",
                "reason": "This request has multiple steps that would benefit from planning first.",
            })

            assert isinstance(result, Command)
            messages = result.update["messages"]
            assert len(messages) == 1
            assert isinstance(messages[0], ToolMessage)
            assert "yes, switch to plan mode" in messages[0].content
            assert messages[0].tool_call_id == "tool_call_abc"

    def test_suggest_plan_mode_captures_no_response(self):
        """When user declines, the tool should return their decision."""
        from app.agents.leonardo.rails_beginner_agent.nodes import suggest_plan_mode

        with patch(
            "app.agents.leonardo.rails_beginner_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "no, continue in beginner mode"

            mock_runtime = MagicMock()
            mock_runtime.tool_call_id = "tool_call_xyz"

            result = suggest_plan_mode.func(
                reason="Your request is complex.",
                runtime=mock_runtime,
            )

            messages = result.update["messages"]
            assert "no, continue in beginner mode" in messages[0].content

    def test_suggest_plan_mode_in_default_tools(self):
        """suggest_plan_mode should be registered in the default_tools list."""
        from app.agents.leonardo.rails_beginner_agent.nodes import default_tools

        tool_names = [t.name for t in default_tools]
        assert "suggest_plan_mode" in tool_names


class TestCheckAndSendInterruptsSuggestModeSwitch:
    """Test that _check_and_send_interrupts handles suggest_mode_switch with original_message."""

    @pytest.mark.asyncio
    async def test_suggest_mode_switch_includes_original_message(self):
        """When graph has a suggest_mode_switch interrupt, WS message must include original_message."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "suggest_mode_switch",
            "target_mode": "plan",
            "reason": "This looks complex.",
        }

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        config = {"configurable": {"thread_id": "t1"}}
        message_data = {
            "thread_id": "thread_123",
            "agent_name": "beginner",
            "message": "Build me a full e-commerce site with cart, checkout, and admin panel",
        }

        result = await handler._check_and_send_interrupts(
            mock_app, config, message_data, mock_websocket
        )

        assert result is True
        mock_websocket.send_json.assert_called_once_with({
            "type": "suggest_mode_switch",
            "target_mode": "plan",
            "reason": "This looks complex.",
            "original_message": "Build me a full e-commerce site with cart, checkout, and admin panel",
            "thread_id": "thread_123",
            "agent_name": "beginner",
        })

    @pytest.mark.asyncio
    async def test_suggest_mode_switch_original_message_defaults_to_empty_string(self):
        """If message_data has no 'message' key, original_message should default to ''."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "suggest_mode_switch",
            "target_mode": "plan",
            "reason": "Complex request.",
        }

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        message_data = {
            "thread_id": "t1",
            "agent_name": "beginner",
            # no 'message' key
        }

        await handler._check_and_send_interrupts(mock_app, {}, message_data, mock_websocket)

        sent = mock_websocket.send_json.call_args[0][0]
        assert sent["original_message"] == ""
        assert sent["type"] == "suggest_mode_switch"

    @pytest.mark.asyncio
    async def test_suggest_mode_switch_returns_true(self):
        """_check_and_send_interrupts should return True when a suggest_mode_switch interrupt is found."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        mock_interrupt = MagicMock()
        mock_interrupt.value = {"type": "suggest_mode_switch", "target_mode": "plan", "reason": ""}

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        message_data = {"thread_id": "t1", "agent_name": "beginner", "message": "hello"}

        result = await handler._check_and_send_interrupts(mock_app, {}, message_data, mock_websocket)

        assert result is True
