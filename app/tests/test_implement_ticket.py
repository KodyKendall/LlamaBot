"""
Tests for the implement-ticket flow:
- offer_implementation tool fires the correct interrupt
- _check_and_send_interrupts sends implement_ticket to the frontend
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langgraph.types import interrupt, Command
from langchain_core.messages import ToolMessage


class TestOfferImplementationTool:
    """Test the offer_implementation tool in ticket mode agent."""

    def test_offer_implementation_fires_interrupt(self):
        """offer_implementation should call interrupt() with implement_ticket type."""
        from app.agents.leonardo.rails_ticket_mode_agent.nodes import offer_implementation

        # The tool function calls interrupt() which raises a special exception
        # in LangGraph. We mock interrupt to capture what it receives.
        with patch(
            "app.agents.leonardo.rails_ticket_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "yes"

            mock_runtime = MagicMock()
            mock_runtime.tool_call_id = "test_tool_call_123"

            result = offer_implementation.func(
                ticket_id="42",
                ticket_title="2025-01-25 - BUG: Test Ticket",
                ticket_content="Full ticket content here",
                runtime=mock_runtime,
            )

            # Verify interrupt was called with the right payload
            mock_interrupt.assert_called_once_with({
                "type": "implement_ticket",
                "ticket_id": "42",
                "ticket_title": "2025-01-25 - BUG: Test Ticket",
                "ticket_content": "Full ticket content here",
            })

            # Verify it returns a Command with the user's decision
            assert isinstance(result, Command)
            messages = result.update["messages"]
            assert len(messages) == 1
            assert isinstance(messages[0], ToolMessage)
            assert "yes" in messages[0].content
            assert messages[0].tool_call_id == "test_tool_call_123"

    def test_offer_implementation_captures_no_response(self):
        """When user says no, the tool should return their decision."""
        from app.agents.leonardo.rails_ticket_mode_agent.nodes import offer_implementation

        with patch(
            "app.agents.leonardo.rails_ticket_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "no"

            mock_runtime = MagicMock()
            mock_runtime.tool_call_id = "test_tool_call_456"

            result = offer_implementation.func(
                ticket_id="99",
                ticket_title="Test",
                ticket_content="Content",
                runtime=mock_runtime,
            )

            messages = result.update["messages"]
            assert "no" in messages[0].content

    def test_offer_implementation_in_default_tools(self):
        """offer_implementation should be registered in the default_tools list."""
        from app.agents.leonardo.rails_ticket_mode_agent.nodes import default_tools

        tool_names = [t.name for t in default_tools]
        assert "offer_implementation" in tool_names


class TestCheckAndSendInterrupts:
    """Test that _check_and_send_interrupts handles implement_ticket type."""

    @pytest.mark.asyncio
    async def test_implement_ticket_interrupt_sent_to_websocket(self):
        """When graph has an implement_ticket interrupt, it should send the right WS message."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}

        # Mock the app with a state snapshot that has an implement_ticket interrupt
        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "implement_ticket",
            "ticket_id": "42",
            "ticket_title": "2025-01-25 - BUG: Test Ticket",
            "ticket_content": "Full ticket content here",
        }

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        mock_websocket.client_state = MagicMock()
        # Make _is_websocket_open return True
        handler._is_websocket_open = MagicMock(return_value=True)

        config = {"configurable": {"thread_id": "test_thread"}}
        message_data = {
            "thread_id": "test_thread_123",
            "agent_name": "rails_ticket_mode_agent",
        }

        result = await handler._check_and_send_interrupts(
            mock_app, config, message_data, mock_websocket
        )

        assert result is True
        mock_websocket.send_json.assert_called_once_with({
            "type": "implement_ticket",
            "ticket_id": "42",
            "ticket_title": "2025-01-25 - BUG: Test Ticket",
            "ticket_content": "Full ticket content here",
            "thread_id": "test_thread_123",
            "agent_name": "rails_ticket_mode_agent",
        })

    @pytest.mark.asyncio
    async def test_no_interrupt_returns_false(self):
        """When there are no interrupts, should return False."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = []

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        config = {"configurable": {"thread_id": "test_thread"}}
        message_data = {"thread_id": "t1", "agent_name": "rails_ticket_mode_agent"}

        result = await handler._check_and_send_interrupts(
            mock_app, config, message_data, mock_websocket
        )

        assert result is False
        mock_websocket.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_implement_ticket_includes_all_fields(self):
        """Ensure ticket_content is passed through (can be large)."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        large_content = "## Description\n" + ("Line of content\n" * 500)

        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "implement_ticket",
            "ticket_id": "7",
            "ticket_title": "Big Ticket",
            "ticket_content": large_content,
        }

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        message_data = {"thread_id": "t1", "agent_name": "rails_ticket_mode_agent"}

        await handler._check_and_send_interrupts(
            mock_app, {}, message_data, mock_websocket
        )

        sent_data = mock_websocket.send_json.call_args[0][0]
        assert sent_data["ticket_content"] == large_content
        assert sent_data["type"] == "implement_ticket"
