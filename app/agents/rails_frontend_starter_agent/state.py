from langgraph.prebuilt.chat_agent_executor import AgentState
from typing import NotRequired, Annotated
from typing import Literal
from typing_extensions import TypedDict
import operator

class Todo(TypedDict):
    """Todo to track."""
    content: str
    status: Literal["pending", "in_progress", "completed"]

class RailsAgentState(AgentState):
    todos: Annotated[NotRequired[list[Todo]], operator.add]
    debug_info: NotRequired[dict[str, any]]
    agent_mode: NotRequired[str]
    llm_model: NotRequired[str]
    failed_tool_calls_count: Annotated[NotRequired[int], operator.add]