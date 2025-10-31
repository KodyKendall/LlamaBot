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

from app.agents.utils.playwright_screenshot import capture_page_and_img_src

from openai import OpenAI
from app.agents.utils.images import encode_image

from app.agents.rails_agent.state import RailsAgentState
from app.agents.rails_agent.tools import (
    write_todos, write_file, read_file, ls, edit_file, search_file, bash_command,
    ls_agents, read_agent_file, write_agent_file, edit_agent_file,
    read_langgraph_json, edit_langgraph_json
)
from app.agents.rails_ai_builder_agent.prompts import RAILS_AI_BUILDER_AGENT_PROMPT

import logging
logger = logging.getLogger(__name__)


# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Global tools list

# System message
sys_msg = {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": f"{RAILS_AI_BUILDER_AGENT_PROMPT}",
                "cache_control": {"type": "ephemeral"}, # only works for Anthropic models.
            },
        ],
    }

default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file, search_file, bash_command,
    # Agent file tools
    ls_agents, read_agent_file, write_agent_file, edit_agent_file,
    read_langgraph_json, edit_langgraph_json
]

# Helper function to get LLM based on user selection
def get_llm(model_name: str):
   """Get LLM instance based on model name from frontend"""
   if model_name == "gpt-5-codex":
      return ChatOpenAI(
         model="gpt-5-codex",
         use_responses_api=True,
         reasoning={"effort": "low"}
      )
   elif model_name == "claude-4.5-sonnet":
      return ChatAnthropic(model="claude-sonnet-4-5-20250929", max_tokens=16384)
   elif model_name == "claude-4.5-haiku":
      return ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)
   else:
      # Default to Claude 4.5 Haiku
      return ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)

# Node
def leonardo_ai_builder(state: RailsAgentState) -> Command[Literal["tools"]]:
   # ==================== LLM Model Selection ====================
   # Get model selection from state (passed from frontend)
   llm_model = state.get('llm_model', 'claude-4.5-haiku')
   logger.info(f"🤖 Using LLM model: {llm_model}")
   llm = get_llm(llm_model)
   # =============================================================

   view_path = (state.get('debug_info') or {}).get('view_path')

   messages = [sys_msg] + state["messages"]

   if view_path:
      messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user is currently viewing their Ruby on Rails webpage route at: " + view_path + " </NOTE_FROM_SYSTEM>")]

   # Tools
   tools = [
      write_todos,
      ls, read_file, write_file, edit_file, search_file, bash_command,
      # Agent file tools
      ls_agents, read_agent_file, write_agent_file, edit_agent_file,
      read_langgraph_json, edit_langgraph_json
   ]

   failed_tool_calls_count = state.get("failed_tool_calls_count", 0)
   if failed_tool_calls_count >= 3:
      messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user has had too many failed tool calls. DO NOT DO ANY NEW TOOL CALLS. Tell the user it's failed, and you need to stop and ask the user to try again in a different way. </NOTE_FROM_SYSTEM>")]
      # Don't bind tools when we've failed too many times - we want a text response only
      response = llm.invoke(messages, cache_control={"type": "ephemeral"})
      # Reset counter by subtracting current count (since reducer uses operator.add)
      return {"messages": [response], "failed_tool_calls_count": -failed_tool_calls_count} # by adding a negative number, we subtract the current count and reset it to 0.

   llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)
   response = llm_with_tools.invoke(messages, cache_control={"type": "ephemeral"})
   return {"messages": [response]}

# Graph
def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)

    # Define nodes: these do the work
    builder.add_node("leonardo_ai_builder", leonardo_ai_builder)
    builder.add_node("tools", ToolNode(default_tools))
    
    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "leonardo_ai_builder")

    builder.add_conditional_edges(
        "leonardo_ai_builder",
        tools_condition,
        {"tools": "tools", END: END},
    )

    builder.add_edge("tools", "leonardo_ai_builder")

    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph