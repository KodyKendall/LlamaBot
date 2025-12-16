from langchain_anthropic import ChatAnthropic

from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph, END
from langgraph.types import Command
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode

from pathlib import Path
from typing import Literal
from datetime import date

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, write_file, read_file, ls, edit_file, search_file, bash_command
)
from app.agents.leonardo.rails_ticket_mode_agent.prompts import TICKET_MODE_AGENT_PROMPT

import logging
logger = logging.getLogger(__name__)

# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'


def get_system_message():
    """Build system message with current date appended.

    Date is appended at the end so the main prompt can be cached,
    and only the date portion changes daily.
    """
    current_date = date.today().strftime("%Y-%m-%d")
    prompt_with_date = f"{TICKET_MODE_AGENT_PROMPT}\n\n---\n**Today's Date:** {current_date}\n(Use this date when creating ticket filenames: {current_date}_TYPE_description.md)"

    return {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": prompt_with_date,
                "cache_control": {"type": "ephemeral"},  # only works for Anthropic models.
            },
        ],
    }

# Tools for Ticket Mode - NO internet_search (codebase research only)
default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file, search_file, bash_command
]


# Helper function to get LLM based on user selection
def get_llm(model_name: str):
    """Get LLM instance based on model name from frontend."""
    # Default to Claude 4.5 Haiku for ticket mode
    return ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)


# Node
def leonardo_ticket_mode(state: RailsAgentState) -> Command[Literal["tools"]]:
    # ==================== LLM Model Selection ====================
    # Get model selection from state (passed from frontend)
    llm_model = state.get('llm_model', 'claude-4.5-haiku')
    logger.info(f"Ticket Mode - Using LLM model: {llm_model}")
    llm = get_llm(llm_model)
    # =============================================================

    view_path = (state.get('debug_info') or {}).get('view_path')

    # Get system message with current date
    sys_msg = get_system_message()
    messages = [sys_msg] + state["messages"]

    if view_path:
        messages = messages + [HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user is currently viewing their Ruby on Rails webpage route at: " + view_path + " </NOTE_FROM_SYSTEM>"
        )]

    # Tools for ticket mode
    tools = [
        write_todos,
        ls, read_file, write_file, edit_file, search_file, bash_command
    ]

    # Handle failed tool calls
    failed_tool_calls_count = state.get("failed_tool_calls_count", 0)
    if failed_tool_calls_count >= 3:
        messages = messages + [HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user has had too many failed tool calls. DO NOT DO ANY NEW TOOL CALLS. Tell the user it's failed, and you need to stop and ask the user to try again in a different way. </NOTE_FROM_SYSTEM>"
        )]
        # Don't bind tools when we've failed too many times - we want a text response only
        response = llm.invoke(messages, cache_control={"type": "ephemeral"})
        # Reset counter by subtracting current count (since reducer uses operator.add)
        return {"messages": [response], "failed_tool_calls_count": -failed_tool_calls_count}

    # Add mode-specific system note
    messages = messages + [HumanMessage(
        content="<NOTE_FROM_SYSTEM> The user is in Ticket Mode. You can READ any file but can ONLY WRITE/EDIT .md files in rails/requirements/. NO CODE CHANGES ALLOWED. Focus on: 1) Gathering complete observation template, 2) Technical research, 3) Creating implementation tickets. </NOTE_FROM_SYSTEM>"
    )]

    llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)
    response = llm_with_tools.invoke(messages)

    return {"messages": [response]}


# Graph
def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)

    # Define nodes: these do the work
    builder.add_node("leonardo_ticket_mode", leonardo_ticket_mode)
    builder.add_node("tools", ToolNode(default_tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "leonardo_ticket_mode")

    builder.add_conditional_edges(
        "leonardo_ticket_mode",
        tools_condition,
        {"tools": "tools", END: END},
    )

    builder.add_edge("tools", "leonardo_ticket_mode")

    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph
