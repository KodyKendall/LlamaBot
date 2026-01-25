import pytest
from unittest.mock import MagicMock

# Import the build_workflow function from the correct module
from app.agents.llamapress import nodes

# TODO: Fix test - LangGraph now validates checkpointers must be BaseCheckpointSaver instances
# def test_build_workflow_returns_compiled_workflow():
#     """Test that build_workflow returns a compiled workflow object when given a mock checkpointer."""
#     mock_checkpointer = MagicMock()
#     workflow = nodes.build_workflow(checkpointer=mock_checkpointer)
#     assert workflow is not None