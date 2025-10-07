"""
Tests for the main FastAPI application endpoints.
"""
import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from main import app


class TestMainEndpoints:
    """Test the main application endpoints."""
    
    @pytest.mark.asyncio
    @patch("builtins.open")
    async def test_root_endpoint(self, mock_open, async_client):
        """Test the root endpoint returns HTML."""
        mock_open.return_value.__enter__.return_value.read.return_value = "<html><body><h1>Mock home.html</h1></body></html>"
        response = await async_client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
    
    @pytest.mark.asyncio
    @patch("builtins.open")
    async def test_chat_endpoint(self, mock_open, async_client):
        """Test the chat endpoint returns HTML."""
        mock_open.return_value.__enter__.return_value.read.return_value = "<html><body><h1>Mock chat.html</h1></body></html>"
        response = await async_client.get("/chat")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
    
    @pytest.mark.asyncio
    @patch("builtins.open")
    async def test_page_endpoint(self, mock_open, async_client):
        """Test the page endpoint returns HTML."""
        mock_open.return_value.__enter__.return_value.read.return_value = "<html><body><h1>Mock ../page.html</h1></body></html>"
        response = await async_client.get("/page")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
    
    @pytest.mark.asyncio
    @patch("builtins.open")
    async def test_conversations_endpoint(self, mock_open, async_client):
        """Test the conversations endpoint returns HTML."""
        mock_open.return_value.__enter__.return_value.read.return_value = "<html><body><h1>Mock conversations.html</h1></body></html>"
        response = await async_client.get("/conversations")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
    
    @pytest.mark.asyncio
    @patch("json.load")
    @patch("builtins.open")
    async def test_available_agents_endpoint(self, mock_open, mock_json_load, async_client):
        """Test the available agents endpoint returns JSON."""
        mock_langgraph_json = {
            "graphs": {
                "agent1": {},
                "agent2": {},
                "react_agent": {}
            }
        }
        mock_json_load.return_value = mock_langgraph_json
        
        response = await async_client.get("/available-agents")
        assert response.status_code == 200
        assert response.headers.get("content-type") == "application/json"
        
        data = response.json()
        assert "agents" in data
        assert len(data["agents"]) == 3
        assert "agent1" in data["agents"]
        assert "agent2" in data["agents"]
        assert "react_agent" in data["agents"]


class TestChatMessage:
    """Test the chat message functionality."""
    
    @pytest.mark.asyncio
    async def test_chat_message_basic(self, async_client, mock_build_workflow):
        """Test basic chat message functionality."""
        # Mock the workflow stream
        mock_workflow = mock_build_workflow.return_value
        mock_workflow.stream.return_value = iter([
            ("messages", ("test_message", {"langgraph_node": "test_node"})),
            ("updates", {"test_node": {"messages": []}})
        ])
        
        # Mock HTML file reading
        with patch("builtins.open", mock_open_html("../page.html")):
            chat_data = {
                "message": "Hello, test message",
                "thread_id": "test_thread",
                "agent": "test_agent"
            }
            
            response = await async_client.post("/llamabot-chat-message", json=chat_data)
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
    
    @pytest.mark.asyncio
    async def test_chat_message_without_thread_id(self, async_client, mock_build_workflow):
        """Test chat message without thread_id (should default to '5')."""
        mock_workflow = mock_build_workflow.return_value
        mock_workflow.stream.return_value = iter([])
        
        with patch("builtins.open", mock_open_html("../page.html")):
            chat_data = {
                "message": "Hello without thread_id"
            }
            
            response = await async_client.post("/llamabot-chat-message", json=chat_data)
            assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_chat_message_streaming_response(self, async_client, mock_build_workflow):
        """Test that chat message returns proper streaming response format."""
        # Mock the workflow to return specific chunks
        mock_workflow = mock_build_workflow.return_value
        mock_message = MagicMock()
        mock_message.content = "Test response content"
        
        mock_workflow.stream.return_value = iter([
            ("messages", (mock_message, {"langgraph_node": "respond_naturally"})),
            ("updates", {"respond_naturally": {"messages": [mock_message]}})
        ])
        
        with patch("builtins.open", mock_open_html("../page.html")):
            chat_data = {
                "message": "Test streaming message",
                "thread_id": "test_stream_thread"
            }
            
            response = await async_client.post("/llamabot-chat-message", json=chat_data)
            assert response.status_code == 200
            
            # The response should be a streaming response
            content = response.read()
            assert content is not None


class TestThreadsAndHistory:
    """Test threads and chat history endpoints."""
    
    @pytest.mark.asyncio
    async def test_threads_endpoint(self, async_client, mock_build_workflow):
        """Test the threads endpoint."""
        # Mock checkpointer list method
        mock_checkpoints = [
            ({"configurable": {"thread_id": "thread1"}}, None, None, None, {}),
            ({"configurable": {"thread_id": "thread2"}}, None, None, None, {}),
            ({"configurable": {"thread_id": "thread1"}}, None, None, None, {}),  # Duplicate
        ]
        
        mock_workflow = mock_build_workflow.return_value
        mock_workflow.get_state.return_value = {"messages": []}
        
        with patch("main.get_or_create_checkpointer") as mock_checkpointer:
            mock_checkpointer.return_value.list.return_value = iter(mock_checkpoints)
            
            response = await async_client.get("/threads")
            assert response.status_code == 200
            
            data = response.json()
            assert isinstance(data, list)
            # Should have 2 unique threads
            assert len(data) == 2
    
    @pytest.mark.asyncio
    async def test_chat_history_endpoint(self, async_client, mock_build_workflow):
        """Test the chat history endpoint for a specific thread."""
        mock_state = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"}
            ]
        }
        
        mock_workflow = mock_build_workflow.return_value
        mock_workflow.get_state.return_value = mock_state
        
        response = await async_client.get("/chat-history/test_thread_123")
        assert response.status_code == 200
        
        data = response.json()
        assert "messages" in data
        assert len(data["messages"]) == 2


# Helper functions for mocking file operations
def mock_open_html(filename):
    """Mock open function for HTML files."""
    def mock_open(*args, **kwargs):
        mock_file = MagicMock()
        mock_file.read.return_value = f"<html><body><h1>Mock {filename}</h1></body></html>"
        mock_file.__enter__.return_value = mock_file
        return mock_open 