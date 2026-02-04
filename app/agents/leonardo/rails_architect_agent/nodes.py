from langchain_openai import ChatOpenAI
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

import asyncio
from pathlib import Path
import os
from typing import List, Literal, Optional, TypedDict

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, write_file, read_file, ls, edit_file, search_file,
    # Note: bash_command is NOT included - no code execution allowed
)
from app.agents.leonardo.rails_architect_agent.prompts import ARCHITECT_AGENT_PROMPT

import logging
logger = logging.getLogger(__name__)

# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# System message with cache control for Anthropic models
sys_msg = {
    "role": "system",
    "content": [
        {
            "type": "text",
            "text": f"{ARCHITECT_AGENT_PROMPT}",
            "cache_control": {"type": "ephemeral"},  # Only works for Anthropic models
        },
    ],
}

# Define tools available to this agent
# Limited set - read-focused with write only for .md reports
default_tools = [
    write_todos,
    ls,
    read_file,
    search_file,
    write_file,  # Restricted via system message to rails/architecture/ only
    edit_file,   # Restricted via system message to rails/architecture/ only
    # NO bash_command - no code execution allowed
]

# Helper function to get LLM based on user selection
def get_llm(model_name: str):
    """Get LLM instance based on model name from frontend."""
    if model_name == "gpt-5-codex":
        return ChatOpenAI(
            model="gpt-5-codex",
            use_responses_api=True,
            reasoning={"effort": "low"}
        )
    elif model_name == "claude-4.5-sonnet":
        return ChatAnthropic(model="claude-sonnet-4-5-20250929", max_tokens=16384)
    # Default to Claude 4.5 Haiku
    return ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)


# Main node function
def leonardo_architect(state: RailsAgentState) -> Command[Literal["tools"]]:
    # ==================== LLM Model Selection ====================
    llm_model = state.get('llm_model', 'claude-4.5-haiku')
    logger.info(f"Architect Mode - Using LLM model: {llm_model}")
    llm = get_llm(llm_model)
    # =============================================================

    # Get view context if available
    view_path = (state.get('debug_info') or {}).get('view_path')

    messages = [sys_msg] + state["messages"]

    # Inject view context if user is viewing a Rails page
    if view_path:
        messages = messages + [HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user is currently viewing their Ruby on Rails webpage route at: " + view_path + " </NOTE_FROM_SYSTEM>"
        )]

    # Define tools for this agent - analysis focused
    tools = [
        write_todos,
        ls,
        read_file,
        search_file,
        write_file,
        edit_file,
    ]

    # Handle failed tool calls - stop after 3 failures
    failed_tool_calls_count = state.get("failed_tool_calls_count", 0)
    if failed_tool_calls_count >= 3:
        messages = messages + [HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user has had too many failed tool calls. DO NOT DO ANY NEW TOOL CALLS. Tell the user it's failed, and you need to stop and ask the user to try again in a different way. </NOTE_FROM_SYSTEM>"
        )]
        response = llm.invoke(messages, cache_control={"type": "ephemeral"})
        # Reset counter by subtracting current count (since reducer uses operator.add)
        return {"messages": [response], "failed_tool_calls_count": -failed_tool_calls_count}

    # Inject Architect Mode restriction
    messages = messages + [HumanMessage(
        content="<NOTE_FROM_SYSTEM> You are in Architect Mode. You can READ any file but can ONLY WRITE/EDIT .md files in rails/architecture/. NO CODE CHANGES ALLOWED. Generate audit reports and recommendations only. Analyze both Rails code (app/models, app/controllers, etc.) AND Python/LangGraph agent code (app/agents/). </NOTE_FROM_SYSTEM>"
    )]

    llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)
    response = llm_with_tools.invoke(messages, cache_control={"type": "ephemeral"})

    return {"messages": [response]}


# Graph builder function - MUST be named build_workflow
def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)

    # Add nodes
    builder.add_node("leonardo_architect", leonardo_architect)
    builder.add_node("tools", ToolNode(default_tools))

    # Define edges
    builder.add_edge(START, "leonardo_architect")

    # Conditional routing: either call tools or end
    builder.add_conditional_edges(
        "leonardo_architect",
        tools_condition,
        {"tools": "tools", END: END},
    )

    # Loop back from tools to agent
    builder.add_edge("tools", "leonardo_architect")

    # Compile with checkpointer for conversation persistence
    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph
