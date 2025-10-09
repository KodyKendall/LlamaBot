"""
Test configuration and fixtures for the FastAPI application.
"""
import pytest
import pytest_asyncio
import asyncio
import os
from httpx import AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch

# Disable LangSmith tracing completely for tests
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("LANGSMITH_ENDPOINT", None)
os.environ.pop("LANGCHAIN_ENDPOINT", None)
os.environ.pop("LANGSMITH_RUNS_ENDPOINTS", None)
os.environ.pop("LANGCHAIN_API_KEY", None)
os.environ.pop("LANGSMITH_API_KEY", None)

# Add the backend directory to the Python path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from main import app, manager
from langgraph.checkpoint.memory import MemorySaver
from main import auth


@pytest.fixture(scope="session", autouse=True)
def disable_langsmith_completely():
    """Completely disable LangSmith tracing for the entire test session."""
    # Store original values
    original_env = {}
    langsmith_vars = [
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_API_KEY", 
        "LANGSMITH_API_KEY",
        "LANGSMITH_ENDPOINT",
        "LANGCHAIN_ENDPOINT"
    ]
    
    for var in langsmith_vars:
        original_env[var] = os.environ.get(var)
    
    # Disable LangSmith
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    for var in langsmith_vars[1:]:  # Skip LANGCHAIN_TRACING_V2 since we want it false
        os.environ.pop(var, None)
    
    yield
    
    # Restore original values (cleanup)
    for var, value in original_env.items():
        if value is not None:
            os.environ[var] = value
        else:
            os.environ.pop(var, None)


@pytest_asyncio.fixture
async def async_client():
    """Create an async HTTP client for testing."""
    app.dependency_overrides[auth] = lambda: "testuser"
    
    from fastapi.testclient import TestClient
    
    with TestClient(app) as client:
        # We need to patch the client to work with our FastAPI app
        from fastapi.testclient import TestClient
        sync_client = TestClient(app)

        # Create a wrapper that mimics AsyncClient but uses TestClient under the hood
        class AsyncClientWrapper:
            def __init__(self, sync_client):
                self._sync_client = sync_client

            async def get(self, url, **kwargs):
                return self._sync_client.get(url, **kwargs)

            async def post(self, url, **kwargs):
                return self._sync_client.post(url, **kwargs)

            async def put(self, url, **kwargs):
                return self._sync_client.put(url, **kwargs)

            async def delete(self, url, **kwargs):
                return self._sync_client.delete(url, **kwargs)

            async def patch(self, url, **kwargs):
                return self._sync_client.patch(url, **kwargs)

        yield AsyncClientWrapper(sync_client)
    
    app.dependency_overrides = {}


@pytest.fixture
def mock_checkpointer():
    """Mock checkpointer for testing."""
    return MemorySaver()


@pytest.fixture
def mock_llm():
    """Mock LLM client for testing."""
    mock = AsyncMock()
    mock.stream = AsyncMock()
    return mock


@pytest.fixture
def mock_langsmith_client():
    """Mock LangSmith client for testing."""
    return MagicMock()


@pytest.fixture
def mock_websocket_manager():
    """Mock WebSocket connection manager."""
    mock_manager = MagicMock()
    mock_manager.connect = AsyncMock()
    mock_manager.disconnect = AsyncMock()
    mock_manager.send_personal_message = AsyncMock()
    mock_manager.broadcast = AsyncMock()
    return mock_manager


@pytest.fixture
def sample_chat_message():
    """Sample chat message for testing."""
    return {
        "message": "Hello, this is a test message",
        "thread_id": "test_thread_123",
        "agent": "test_agent"
    }


@pytest.fixture
def mock_html_content():
    """Mock HTML content for testing."""
    return "<html><body><h1>Test Page</h1></body></html>"


@pytest.fixture
def mock_workflow():
    """Mock LangGraph workflow for testing."""
    mock = MagicMock()
    mock.stream = MagicMock()
    mock.get_state = MagicMock()
    return mock


@pytest.fixture(autouse=True)
def setup_test_environment():
    """Set up test environment variables and mocks."""
    # Set test environment variables
    # Note: We don't set LANGSMITH_API_KEY to keep tracing disabled
    os.environ["OPENAI_API_KEY"] = "test_openai_key"
    
    # Ensure LangSmith tracing stays disabled
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    
    # Mock the checkpointer to avoid database connections during tests
    with patch('main.get_or_create_checkpointer') as mock_checkpointer:
        mock_checkpointer.return_value = MemorySaver()
        yield


@pytest.fixture
def mock_database():
    """Mock database connection for testing."""
    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.fetchall = AsyncMock(return_value=[])
    mock_db.fetchone = AsyncMock(return_value=None)
    return mock_db


@pytest.fixture
def mock_build_workflow():
    """Mock build_workflow function for testing."""
    with patch('main.build_workflow') as mock:
        workflow = MagicMock()
        workflow.stream = MagicMock(return_value=iter([]))
        workflow.get_state = MagicMock(return_value={"messages": []})
        mock.return_value = workflow
        yield mock 