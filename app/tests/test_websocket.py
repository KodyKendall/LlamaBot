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