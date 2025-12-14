from langchain.agents import AgentState
from typing import NotRequired, Annotated, Any
from typing import Literal
from typing_extensions import TypedDict
import operator

class Todo(TypedDict):
    """A structured task item for tracking progress through complex workflows.

    Attributes:
        content: Short, specific description of the task
        status: Current state - pending, in_progress, or completed
    """

    content: str
    status: Literal["pending", "in_progress", "completed"]

class RailsAgentState(AgentState):
    todos: Annotated[NotRequired[list[Todo]], operator.add] # why did claude code change to annotated.?
    debug_info: NotRequired[dict[str, Any]]
    agent_mode: NotRequired[str]
    llm_model: NotRequired[str]
    failed_tool_calls_count: Annotated[NotRequired[int], operator.add]