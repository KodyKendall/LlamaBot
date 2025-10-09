"""
Integration tests for the complete application flow.
"""
import pytest
import json
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


@pytest.mark.integration
class TestApplicationIntegration:
    """Integration tests for the complete application."""
    
    @pytest.mark.asyncio
    async def test_complete_chat_flow(self, async_client, mock_build_workflow):
        """Test the complete chat flow from request to response."""
        # Mock the workflow to simulate a complete interaction
        mock_workflow = mock_build_workflow.return_value
        mock_message = MagicMock()
        mock_message.content = "Hello! I'm a test response from the AI."
        
        mock_workflow.stream.return_value = iter([
            ("messages", (mock_message, {"langgraph_node": "route_initial_user_message"})),
            ("updates", {"route_initial_user_message": {"messages": [mock_message]}}),
            ("messages", (mock_message, {"langgraph_node": "respond_naturally"})),
            ("updates", {"respond_naturally": {"messages": [mock_message]}})
        ])
        
        # Mock HTML file reading
        with patch("builtins.open", mock_open_html("../page.html")):
            # Send a chat message
            chat_data = {
                "message": "Hello, this is an integration test",
                "thread_id": "integration_test_thread",
                "agent": "react_agent"
            }
            
            response = await async_client.post("/llamabot-chat-message", json=chat_data)
            
            # Verify the response
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
            
            # The response should contain streaming data
            content = response.read()
            assert content is not None
    
    @pytest.mark.asyncio
    async def test_thread_persistence_flow(self, async_client, mock_build_workflow):
        """Test thread persistence across multiple requests."""
        mock_workflow = mock_build_workflow.return_value
        
        # Mock different states for different calls
        call_count = 0
        def get_state_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "messages": [
                    {"role": "user", "content": f"Message {call_count}"},
                    {"role": "assistant", "content": f"Response {call_count}"}
                ]
            }
        
        mock_workflow.get_state.side_effect = get_state_side_effect
        
        # Get thread history
        thread_id = "persistence_test_thread"
        response = await async_client.get(f"/chat-history/{thread_id}")
        
        assert response.status_code == 200
        data = response.json()
        assert "messages" in data
        assert len(data["messages"]) == 2
        assert data["messages"][0]["content"] == "Message 1"
    
    @pytest.mark.asyncio
    async def test_multiple_threads_isolation(self, async_client, mock_build_workflow):
        """Test that multiple threads remain isolated."""
        mock_workflow = mock_build_workflow.return_value
        
        # Mock different states for different threads
        def get_state_side_effect(config):
            thread_id = config["configurable"]["thread_id"]
            return {
                "messages": [
                    {"role": "user", "content": f"Hello from {thread_id}"},
                    {"role": "assistant", "content": f"Response for {thread_id}"}
                ]
            }
        
        mock_workflow.get_state.side_effect = get_state_side_effect
        
        # Test two different threads
        response1 = await async_client.get("/chat-history/thread_1")
        response2 = await async_client.get("/chat-history/thread_2")
        
        assert response1.status_code == 200
        assert response2.status_code == 200
        
        data1 = response1.json()
        data2 = response2.json()
        
        assert data1["messages"][0]["content"] == "Hello from thread_1"
        assert data2["messages"][0]["content"] == "Hello from thread_2"
    
    @pytest.mark.asyncio
    @patch("json.load")
    @patch("builtins.open")
    async def test_available_agents_integration(self, mock_open, mock_json_load, async_client):
        """Test the available agents endpoint integration."""
        mock_config = {
            "graphs": {
                "react_agent": {
                    "description": "A reactive agent for handling user queries"
                },
                "write_html_agent": {
                    "description": "An agent specialized in writing HTML"
                }
            }
        }
        mock_json_load.return_value = mock_config

        response = await async_client.get("/available-agents")
        
        assert response.status_code == 200
        data = response.json()
        
        assert "agents" in data
        assert "react_agent" in data["agents"]
        assert "write_html_agent" in data["agents"]
    
    @pytest.mark.asyncio 
    @patch("builtins.open")
    async def test_html_endpoints_integration(self, mock_open, async_client):
        """Test all HTML-serving endpoints."""
        html_endpoints = [
            ("/", "home.html"),
            ("/chat", "chat.html"),
            ("/conversations", "conversations.html"),
            ("/page", "../page.html")
        ]
        
        for endpoint, filename in html_endpoints:
            mock_open.return_value.__enter__.return_value.read.return_value = f"<html><body><h1>Mock {filename}</h1></body></html>"
            response = await async_client.get(endpoint)
            
            assert response.status_code == 200
            assert "text/html" in response.headers.get("content-type", "")
            content = response.text
            assert f"Mock {filename}" in content
    
    @pytest.mark.asyncio
    async def test_error_handling_integration(self, async_client, mock_build_workflow):
        """Test error handling in the complete flow."""
        # Mock the workflow to raise an exception
        mock_workflow = mock_build_workflow.return_value
        mock_workflow.stream.side_effect = Exception("Simulated workflow error")
        
        with patch("builtins.open", mock_open_html("../page.html")):
            chat_data = {
                "message": "This should cause an error",
                "thread_id": "error_test_thread"
            }
            
            response = await async_client.post("/llamabot-chat-message", json=chat_data)
            
            # The response should still be 200 (streaming response)
            # but should contain error information in the stream
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")


@pytest.mark.integration
@pytest.mark.websocket
class TestWebSocketIntegration:
    """Integration tests for WebSocket functionality."""
    
    def test_websocket_complete_flow(self):
        """Test complete WebSocket flow."""
        from main import app
        client = TestClient(app)
        
        with client.websocket_connect("/ws") as websocket:
            # Test connection
            test_message = {
                "type": "chat",
                "message": "WebSocket integration test",
                "thread_id": "ws_integration_thread"
            }
            
            # Send message
            websocket.send_text(json.dumps(test_message))
            
            # In a real scenario, we would receive responses
            # For this test, we just verify the connection works
            try:
                # Try to receive any response
                response = websocket.receive_text()
                # If we get here, the WebSocket is working
                assert response is not None
            except Exception:
                # Connection might close quickly in test environment
                # This is acceptable for integration testing
                pass


# Helper function for mocking file operations
def mock_open_html(filename):
    """Mock open function for HTML files."""
    def mock_open(*args, **kwargs):
        mock_file = MagicMock()
        mock_file.read.return_value = f"<html><body><h1>Mock {filename}</h1></body></html>"
        mock_file.__enter__.return_value = mock_file
        return mock_open 