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


class TestThreadsAndHistory:
    """Test threads and chat history endpoints."""

    @pytest.mark.asyncio
    async def test_threads_endpoint(self, async_client):
        """Test the threads endpoint returns expected format."""
        from datetime import datetime
        from unittest.mock import MagicMock

        # Mock the thread metadata query
        mock_threads = []

        with patch("app.routers.api.get_thread_list") as mock_get_threads:
            mock_get_threads.return_value = mock_threads

            response = await async_client.get("/threads")
            assert response.status_code == 200

            data = response.json()
            assert "threads" in data
            assert "has_more" in data
            assert isinstance(data["threads"], list)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Requires full database and checkpointer setup - tested in integration environment")
    async def test_chat_history_endpoint(self, async_client):
        """Test the chat history endpoint for a specific thread."""
        # This test requires a fully configured database and checkpointer
        # which is not available in the CI test environment
        response = await async_client.get("/chat-history/test_thread_123")
        assert response.status_code == 200


# Helper functions for mocking file operations
def mock_open_html(filename):
    """Mock open function for HTML files."""
    def mock_open(*args, **kwargs):
        mock_file = MagicMock()
        mock_file.read.return_value = f"<html><body><h1>Mock {filename}</h1></body></html>"
        mock_file.__enter__.return_value = mock_file
        return mock_open 