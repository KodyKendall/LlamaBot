from langchain.agents import AgentState
from typing import NotRequired, Annotated, Any
from typing import Literal
from typing_extensions import TypedDict
import operator

from app.agents.utils.delta_state import DeltaMessages

class Todo(TypedDict):
    """A structured task item for tracking progress through complex workflows.

    Attributes:
        content: Short, specific description of the task
        status: Current state - pending, in_progress, or completed
    """

    content: str
    status: Literal["pending", "in_progress", "completed"]

class RailsAgentState(AgentState):
    # DeltaChannel-backed messages: stores incremental deltas + periodic snapshots
    # instead of re-serializing the whole list into every checkpoint (~O(N) not
    # O(N^2) storage). See app/agents/utils/delta_state.py. NOTE: threads using
    # this state must be excluded from per-thread partial checkpoint cleanup.
    messages: DeltaMessages
    todos: Annotated[NotRequired[list[Todo]], operator.add] # why did claude code change to annotated.?
    debug_info: NotRequired[dict[str, Any]]
    agent_mode: NotRequired[str]
    llm_model: NotRequired[str]
    # NOTE: deliberately NO `api_token` here. The built-in agents don't call the
    # user-scoped Rails API, and declaring the field would persist a bearer credential
    # into every checkpoint for nothing. Custom agents that DO use it subclass
    # LlamaPressAPIState (app/lib/llamapress_api.py), which declares it.
    failed_tool_calls_count: Annotated[NotRequired[int], operator.add]