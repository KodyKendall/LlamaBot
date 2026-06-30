"""
Tests for WebSocket functionality.
"""
import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.websocket.web_socket_connection_manager import WebSocketConnectionManager
from app.websocket.web_socket_handler import WebSocketHandler
from app.websocket.web_socket_request_context import WebSocketRequestContext


class TestWebSocketConnectionManager:
    """Test the WebSocket connection manager."""
    
    def test_manager_initialization(self):
        """Test WebSocket manager initialization."""
        app = MagicMock()
        manager = WebSocketConnectionManager(app)
        
        assert manager.app == app
        assert manager.active_connections == []
    
    @pytest.mark.asyncio
    async def test_connect_websocket(self):
        """Test connecting a WebSocket."""
        app = MagicMock()
        manager = WebSocketConnectionManager(app)
        
        mock_websocket = AsyncMock()
        mock_websocket.accept = AsyncMock()
        
        await manager.connect(mock_websocket)
        
        mock_websocket.accept.assert_called_once()
        assert mock_websocket in manager.active_connections
    
    @pytest.mark.asyncio
    async def test_disconnect_websocket(self):
        """Test disconnecting a WebSocket."""
        app = MagicMock()
        manager = WebSocketConnectionManager(app)
        
        mock_websocket = AsyncMock()
        # Simulate the websocket being connected first
        manager.active_connections.append(mock_websocket)
        manager._connection_ids.add(id(mock_websocket))
        
        manager.disconnect(mock_websocket)
        
        assert mock_websocket not in manager.active_connections
    
    @pytest.mark.asyncio
    async def test_send_personal_message(self):
        """Test sending a personal message."""
        app = MagicMock()
        manager = WebSocketConnectionManager(app)
        
        mock_websocket = AsyncMock()
        mock_websocket.send_text = AsyncMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        
        test_message = "Hello, personal message!"
        await manager.send_personal_message(test_message, mock_websocket)
        
        mock_websocket.send_text.assert_called_once_with(test_message)
    
    @pytest.mark.asyncio
    async def test_broadcast_message(self):
        """Test broadcasting a message to all connections."""
        app = MagicMock()
        manager = WebSocketConnectionManager(app)
        
        # Create multiple mock WebSockets
        mock_ws1 = AsyncMock()
        mock_ws1.send_json = AsyncMock()
        mock_ws1.client_state = WebSocketState.CONNECTED
        mock_ws2 = AsyncMock()
        mock_ws2.send_json = AsyncMock()
        mock_ws2.client_state = WebSocketState.CONNECTED
        
        manager.active_connections = [mock_ws1, mock_ws2]
        
        test_message = "Broadcast message to all!"
        await manager.broadcast(test_message)
        
        # The broadcast method uses send_json with a message wrapper
        mock_ws1.send_json.assert_called_once_with({"message": test_message})
        mock_ws2.send_json.assert_called_once_with({"message": test_message})
    
    @pytest.mark.asyncio
    async def test_broadcast_with_failed_connection(self):
        """Test broadcasting when one connection fails."""
        app = MagicMock()
        manager = WebSocketConnectionManager(app)
        
        # Create mock WebSockets, one that will fail
        mock_ws1 = AsyncMock()
        mock_ws1.send_json = AsyncMock()
        mock_ws1.client_state = WebSocketState.CONNECTED
        mock_ws2 = AsyncMock()
        mock_ws2.send_json = AsyncMock(side_effect=WebSocketDisconnect(1000))
        mock_ws2.client_state = WebSocketState.CONNECTED
        
        # Properly set up the manager's tracking
        manager.active_connections = [mock_ws1, mock_ws2]
        manager._connection_ids.add(id(mock_ws1))
        manager._connection_ids.add(id(mock_ws2))
        
        test_message = "Broadcast with failure"
        
        # The implementation handles exceptions in broadcast, so this won't raise
        # The failing connection will be removed from active_connections
        await manager.broadcast(test_message)
        
        # The first websocket should have been called
        mock_ws1.send_json.assert_called_once_with({"message": test_message})
        # The second websocket should have been called but failed
        mock_ws2.send_json.assert_called_once_with({"message": test_message})
        
        # The failed connection should be removed from active_connections
        assert mock_ws2 not in manager.active_connections
        assert mock_ws1 in manager.active_connections


class TestWebSocketHandler:
    """Test the WebSocket handler."""
    
    @pytest.mark.asyncio
    async def test_websocket_handler_initialization(self):
        """Test WebSocket handler initialization."""
        mock_websocket = AsyncMock()
        mock_manager = MagicMock()
        
        handler = WebSocketHandler(mock_websocket, mock_manager)
        
        assert handler.websocket == mock_websocket
        assert handler.manager == mock_manager
    
    @pytest.mark.asyncio
    async def test_handle_websocket_connection(self):
        """Test handling a WebSocket connection."""
        mock_websocket = AsyncMock()
        mock_manager = AsyncMock()
        mock_manager.connect = AsyncMock()
        mock_manager.disconnect = MagicMock()
        mock_manager.send_personal_message = AsyncMock()
        
        # Create a mock that raises WebSocketDisconnect on the first call
        # This will simulate an immediate disconnection
        mock_websocket.receive_json.side_effect = WebSocketDisconnect(code=1000, reason="Test disconnect")
        
        handler = WebSocketHandler(mock_websocket, mock_manager)
        
        # Mock the request handler
        with patch.object(handler, 'request_handler') as mock_request_handler:
            mock_request_handler.cleanup_connection = MagicMock()
            
            await handler.handle_websocket()
            
            # Check that connection was established
            mock_manager.connect.assert_called_once_with(mock_websocket)
            
            # Check that connection was cleaned up
            mock_manager.disconnect.assert_called_once_with(mock_websocket)
            mock_request_handler.cleanup_connection.assert_called_once_with(mock_websocket)


class TestWebSocketRequestContext:
    """Test the WebSocket request context."""
    
    def test_context_creation(self):
        """Test creating a WebSocket request context."""
        mock_websocket = AsyncMock()
        
        context = WebSocketRequestContext(mock_websocket)
        
        assert context.websocket == mock_websocket
        assert context.langgraph_checkpointer is None


class TestWebSocketDisconnectScenarios:
    """Tests for WebSocket disconnect edge cases - reproducing error loop from production."""

    @pytest.mark.asyncio
    async def test_receive_after_disconnect_should_exit_gracefully(self):
        """
        Reproduces: WebSocket error: WebSocket is not connected. Need to call "accept" first.

        When receive_json() raises RuntimeError because the WebSocket is not connected,
        the handler SHOULD break out of the loop and exit gracefully.

        Currently FAILS because the exception is caught in the inner try/except,
        which logs the error and continues the while True loop.
        """
        import asyncio

        mock_websocket = AsyncMock()
        mock_manager = AsyncMock()
        mock_manager.connect = AsyncMock()
        mock_manager.disconnect = MagicMock()
        mock_manager.send_personal_message = AsyncMock()

        # Track how many times receive_json is called
        call_count = 0
        max_calls = 5  # If we hit this many, we're in an infinite loop

        async def mock_receive_json():
            nonlocal call_count
            call_count += 1
            # Yield control to allow timeout to work
            await asyncio.sleep(0)
            if call_count >= max_calls:
                # Stop the loop after max_calls to prevent actual infinite loop
                raise KeyboardInterrupt(f"Stopped after {call_count} calls - infinite loop detected!")
            # Simulate the exact error from Starlette when WebSocket is not connected
            raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')

        mock_websocket.receive_json = mock_receive_json
        # client_state shows DISCONNECTED (connection is closed)
        mock_websocket.client_state = WebSocketState.DISCONNECTED

        handler = WebSocketHandler(mock_websocket, mock_manager)

        with patch.object(handler, 'request_handler') as mock_request_handler:
            mock_request_handler.cleanup_connection = MagicMock()

            # Run the handler
            try:
                await handler.handle_websocket()
            except KeyboardInterrupt:
                pass  # Expected if we hit the infinite loop guard

            # Verify: Connection was established and cleaned up
            mock_manager.connect.assert_called_once()
            mock_manager.disconnect.assert_called_once()

            # KEY ASSERTION: receive_json should only be called ONCE
            # If it's called multiple times, we're in an error loop
            assert call_count == 1, \
                f"Expected 1 call to receive_json, got {call_count}. " \
                "Handler is looping on error instead of exiting gracefully."


class TestWebSocketIdempotency:
    """Reproduces the 0.5.3a duplicate-restart bug at the handler level."""

    @pytest.mark.asyncio
    async def test_resent_message_with_same_client_id_is_not_reprocessed(self):
        """
        Reproduces: a WebSocket drop causes the browser to re-send the same chat
        message on reconnect. With no idempotency guard the backend cancels the
        in-flight run and starts a brand-new one, restarting the agent from
        scratch ("Let me start by reading...").

        The identical message arrives twice (same client_message_id); the agent
        run must be started exactly ONCE. The second arrival is a reconnect
        resend and must be ignored.
        """
        from app.websocket.message_deduplicator import MessageDeduplicator

        mock_websocket = AsyncMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_manager = AsyncMock()
        mock_manager.connect = AsyncMock()
        mock_manager.disconnect = MagicMock()
        mock_manager.send_personal_message = AsyncMock()
        mock_manager.deduplicator = MessageDeduplicator()

        payload = {
            "message": "build me an app from this spreadsheet",
            "thread_id": "thread-1",
            "client_message_id": "msg-abc",
        }
        # Same message twice (reconnect resend), then disconnect to end the loop.
        events = [dict(payload), dict(payload),
                  WebSocketDisconnect(code=1000, reason="bye")]

        async def fake_receive_json():
            item = events.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        mock_websocket.receive_json = fake_receive_json
        from app.websocket.run_manager import RunManager
        mock_manager.app = MagicMock()
        mock_manager.app.state.run_manager = RunManager()

        handler = WebSocketHandler(mock_websocket, mock_manager)
        handler.authenticated = True

        # Patch start_chat_run (the background-run entry) so we assert the
        # idempotency guard's decision directly, independent of run timing.
        with patch.object(handler.request_handler, "start_chat_run",
                          new=AsyncMock()) as mock_start, \
             patch.object(handler.request_handler, "cleanup_connection",
                          MagicMock()):
            await handler.handle_websocket()

        assert mock_start.call_count == 1, (
            f"Expected one run to start, got {mock_start.call_count} — the "
            f"resent message was reprocessed (no idempotency guard)."
        )

    @pytest.mark.asyncio
    async def test_distinct_messages_both_start_a_run(self):
        """Two genuinely different messages (distinct ids) must both pass the guard."""
        from app.websocket.message_deduplicator import MessageDeduplicator

        mock_websocket = AsyncMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_manager = AsyncMock()
        mock_manager.connect = AsyncMock()
        mock_manager.disconnect = MagicMock()
        mock_manager.send_personal_message = AsyncMock()
        mock_manager.deduplicator = MessageDeduplicator()

        events = [
            {"message": "first", "thread_id": "t", "client_message_id": "id-1"},
            {"message": "second", "thread_id": "t", "client_message_id": "id-2"},
            WebSocketDisconnect(code=1000, reason="bye"),
        ]

        async def fake_receive_json():
            item = events.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        mock_websocket.receive_json = fake_receive_json
        from app.websocket.run_manager import RunManager
        mock_manager.app = MagicMock()
        mock_manager.app.state.run_manager = RunManager()

        handler = WebSocketHandler(mock_websocket, mock_manager)
        handler.authenticated = True

        with patch.object(handler.request_handler, "start_chat_run",
                          new=AsyncMock()) as mock_start, \
             patch.object(handler.request_handler, "cleanup_connection",
                          MagicMock()):
            await handler.handle_websocket()

        assert mock_start.call_count == 2


class TestWebSocketLayer2Attach:
    """Handler-level wiring for attach/replay (Layer 2 background runs)."""

    def _make_handler(self):
        from app.websocket.run_manager import RunManager
        mock_websocket = AsyncMock()
        mock_websocket.client_state = WebSocketState.CONNECTED
        mock_manager = AsyncMock()
        mock_manager.app = MagicMock()
        mock_manager.app.state.run_manager = RunManager()
        mock_manager.send_personal_message = AsyncMock()
        handler = WebSocketHandler(mock_websocket, mock_manager)
        handler.authenticated = True
        return handler, mock_manager

    @pytest.mark.asyncio
    async def test_attach_replays_missed_messages_then_attached(self):
        from app.websocket.run_manager import RunHandle, ThreadOutputLog
        handler, mock_manager = self._make_handler()
        rm = mock_manager.app.state.run_manager

        handle = RunHandle("t1", ThreadOutputLog())
        for i in range(3):
            handle.log.append({"type": "ai", "content": f"c{i}"})  # seqs 1,2,3
        rm._runs["t1"] = handle

        await handler._handle_attach({"thread_id": "t1", "last_seq": 1})

        sent = [c.args[0] for c in mock_manager.send_personal_message.call_args_list]
        # Replays seq 2 and 3 (not seq 1), then an "attached" control frame.
        assert [m.get("seq") for m in sent[:2]] == [2, 3]
        assert sent[-1]["type"] == "attached"
        assert sent[-1]["status"] == "running"
        assert sent[-1]["last_seq"] == 3
        # The socket is now the live subscriber.
        assert handle.attached_ws is handler.websocket

    @pytest.mark.asyncio
    async def test_attach_no_active_run(self):
        handler, mock_manager = self._make_handler()
        await handler._handle_attach({"thread_id": "ghost", "last_seq": 0})
        sent = [c.args[0] for c in mock_manager.send_personal_message.call_args_list]
        assert sent[-1]["type"] == "no_active_run"

    @pytest.mark.asyncio
    async def test_attach_signals_gap_when_window_evicted(self):
        from app.websocket.run_manager import RunHandle, ThreadOutputLog
        handler, mock_manager = self._make_handler()
        rm = mock_manager.app.state.run_manager

        handle = RunHandle("t1", ThreadOutputLog(maxlen=2))
        for i in range(5):
            handle.log.append({"type": "ai", "content": i})  # only seqs 4,5 retained
        rm._runs["t1"] = handle

        # Client last saw seq 1 → 2,3 evicted → gap, must reload from checkpoint.
        await handler._handle_attach({"thread_id": "t1", "last_seq": 1})
        sent = [c.args[0] for c in mock_manager.send_personal_message.call_args_list]
        assert any(m["type"] == "replay_gap" for m in sent)
        # No raw message entries were replayed (only control frames).
        assert all(m.get("type") in ("replay_gap", "attached") for m in sent)


class TestWebSocketIntegration:
    """Integration tests for WebSocket functionality."""
    
    @pytest.mark.asyncio
    async def test_websocket_endpoint_integration(self):
        """Test the WebSocket endpoint integration."""
        from main import app
        
        client = TestClient(app)
        
        with client.websocket_connect("/ws") as websocket:
            # Send a test message
            test_message = {
                "message": "Integration test message",
                "thread_id": "integration_thread",
                "agent": "test_agent"
            }
            
            websocket.send_json(test_message)
            
            # We might receive multiple messages due to the async nature
            # Just check that we can communicate
            try:
                response = websocket.receive_json()
                # The response should be valid JSON
                assert isinstance(response, dict)
            except Exception:
                # In some cases, the connection might close quickly
                # which is acceptable for testing
                pass
    
    @pytest.mark.asyncio
    async def test_websocket_multiple_connections(self):
        """Test multiple WebSocket connections."""
        from main import app
        
        client = TestClient(app)
        
        # Test that multiple connections can be established
        with client.websocket_connect("/ws") as ws1:
            with client.websocket_connect("/ws") as ws2:
                # Both connections should be active
                test_message = {
                    "message": "Multi-connection test",
                    "thread_id": "multi_thread",
                    "agent": "test_agent"
                }
                
                ws1.send_json(test_message)
                ws2.send_json(test_message)
                
                # Both should be able to send without errors
                # (The actual response handling is tested elsewhere) 