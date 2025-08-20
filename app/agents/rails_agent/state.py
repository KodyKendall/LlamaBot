from langgraph.prebuilt.chat_agent_executor import AgentState
from typing import NotRequired, Annotated
from typing import Literal
from typing_extensions import TypedDict

class Todo(TypedDict):
    """Todo to track."""

    content: str
    status: Literal["pending", "in_progress", "completed"]

class RailsAgentState(AgentState):
    todos: NotRequired[list[Todo]]
