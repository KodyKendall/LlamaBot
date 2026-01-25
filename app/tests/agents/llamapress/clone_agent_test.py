import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime
import json

# Import the build_workflow function from the correct module
from app.agents.llamapress import clone_agent
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

# TODO: Fix test - LangGraph now validates checkpointers must be BaseCheckpointSaver instances
# def test_build_workflow_returns_compiled_workflow():
#     """Test that build_workflow returns a compiled workflow object when given a mock checkpointer."""
#     mock_checkpointer = MagicMock()
#     workflow = clone_agent.build_workflow(checkpointer=mock_checkpointer)
#     assert workflow is not None

def test_deep_clone_routing():
    """Test that a message containing 'deep clone' routes to url_clone_agent correctly."""
    # Test the router_node function directly
    from app.agents.llamapress.clone_agent import router_node, LlamaPressState
    
    # Create a state with a deep clone message
    test_state = LlamaPressState(
        messages=[HumanMessage(content="Please deep clone https://example.com")],
        api_token="test_token",
        agent_prompt="test prompt",
        page_id="test_page_id",
        current_page_html="<html>current</html>",
        selected_element=None,
        javascript_console_errors=None,
        created_at=datetime.now()
    )
    
    # Call the router node
    result = router_node(test_state)
    
    # Verify it routes to url_clone_agent
    assert result["next"] == "url_clone_agent"

def test_clone_routing():
    """Test that a message containing 'clone' (but not 'deep clone') routes to image_clone_agent correctly."""
    # Test the router_node function directly
    from app.agents.llamapress.clone_agent import router_node, LlamaPressState
    
    # Create a state with a clone message (not deep clone)
    test_state = LlamaPressState(
        messages=[HumanMessage(content="Please clone this image")],
        api_token="test_token",
        agent_prompt="test prompt",
        page_id="test_page_id",
        current_page_html="<html>current</html>",
        selected_element=None,
        javascript_console_errors=None,
        created_at=datetime.now()
    )
    
    # Call the router node
    result = router_node(test_state)
    
    # Verify it routes to image_clone_agent
    assert result["next"] == "image_clone_agent"

@pytest.mark.asyncio
@patch('app.agents.llamapress.clone_agent.ChatOpenAI')
async def test_clone_workflow(mock_chat_openai):
    """Test that a message containing 'clone' (but not 'deep clone') routes through the image_clone_agent path."""
    # Mock the LLM response for image_clone_agent with proper AIMessage (no tool calls)
    mock_llm_instance = MagicMock()
    mock_chat_openai.return_value = mock_llm_instance
    
    mock_response = AIMessage(
        content="I am the image clone agent!",
        response_metadata={"created_at": str(datetime.now())}
    )
    
    mock_bind_tools = MagicMock()
    mock_bind_tools.invoke.return_value = mock_response
    mock_llm_instance.bind_tools.return_value = mock_bind_tools
    
    # Build workflow
    workflow = clone_agent.build_workflow()
    
    # Create initial state
    initial_state = {
        "messages": [HumanMessage(content="Please clone this image")],
        "api_token": "test_token",
        "agent_prompt": "test prompt", 
        "page_id": "test_page_id",
        "current_page_html": "<html>current</html>",
        "selected_element": None,
        "javascript_console_errors": None,
        "created_at": datetime.now()
    }
    
    # Run the workflow using async API
    result = await workflow.ainvoke(initial_state)
    
    # Verify the workflow completed successfully
    assert result is not None
    assert "messages" in result
    assert len(result["messages"]) > 1  # Should have initial message plus response
    
    # Verify the response contains the expected message from image_clone_agent
    final_message = result["messages"][-1]
    assert "I am the image clone agent!" in final_message.content