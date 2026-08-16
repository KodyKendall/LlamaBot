from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph, END
from langgraph.types import Command
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode
from app.agents.utils.tool_output_limits import CappedToolNode

import asyncio
from pathlib import Path
import os
from typing import List, Literal, Optional, TypedDict

from openai import OpenAI
from app.agents.utils.images import encode_image

from app.agents.leonardo.rails_agent.state import RailsAgentState
# The box's resolved default (Muse where the box has a META key, DeepSeek
# where it does not) — never a hardcoded id, or a turn that arrives without
# an explicit llm_model silently ignores the fleet default.
from app.agents.leonardo.model_policy import enabled_default_model
from app.agents.leonardo.rails_agent.tools import (
    write_todos, write_file, read_file, ls, edit_file, glob_files, grep_files, bash_command,
    ls_agents, read_agent_file, write_agent_file, edit_agent_file,
    read_langgraph_json, edit_langgraph_json,
    read_brand_guide, write_brand_guide,
)
from app.agents.leonardo.rails_agent.sub_agents import delegate_research
from app.agents.leonardo.rails_ai_builder_agent.prompts import RAILS_AI_BUILDER_AGENT_PROMPT
from app.agents.leonardo.project_context import build_system_prompt_with_project_context, brand_context_section
from app.agents.leonardo.llm_factory import get_llm, invoke_with_cache, system_message_for_model
from app.agents.leonardo.agent_factory import repair_orphaned_tool_calls_in_messages
from app.agents.leonardo.resilience import invoke_with_transient_retry

import logging
logger = logging.getLogger(__name__)


# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Global tools list

def get_sys_msg():
    """Build system message with project context and prompt caching.

    Loads LEONARDO.md if it exists and appends it to the base prompt.
    """
    # Rebuilt every turn (see get_sys_msg call site), so the brand guide appended
    # here stays live without a restart.
    full_prompt = build_system_prompt_with_project_context(RAILS_AI_BUILDER_AGENT_PROMPT, agent_mode="rails_ai_builder_agent") + brand_context_section()
    return {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": full_prompt,
                "cache_control": {"type": "ephemeral"},  # Only works for Anthropic models.
            },
        ],
    }

default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file, glob_files, grep_files, bash_command,
    delegate_research,  # Read-only sub-agent for codebase investigation
    # Agent file tools
    ls_agents, read_agent_file, write_agent_file, edit_agent_file,
    read_langgraph_json, edit_langgraph_json,
    read_brand_guide, write_brand_guide,  # Brand guide (colors, logos, notes)
]

# Node
def leonardo_ai_builder(state: RailsAgentState) -> Command[Literal["tools"]]:
   # ==================== LLM Model Selection ====================
   # Get model selection from state (passed from frontend)
   llm_model = state.get('llm_model') or enabled_default_model()
   logger.info(f"🤖 Using LLM model: {llm_model}")
   llm = get_llm(llm_model)
   # =============================================================

   view_path = (state.get('debug_info') or {}).get('view_path')

   messages = [system_message_for_model(get_sys_msg(), llm_model)] + state["messages"]

   if view_path:
      messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user is currently viewing their Ruby on Rails webpage route at: " + view_path + " </NOTE_FROM_SYSTEM>")]

   # Repair orphaned tool calls before any .invoke() below. This raw StateGraph
   # node never runs AgentMiddleware, so it can't rely on the repair middleware —
   # without this a dangling AIMessage tool_call 400s every later turn
   # ('insufficient tool messages'). SI#112. Covers all cache_control/tools
   # branches since only HumanMessages are appended after this point.
   messages = repair_orphaned_tool_calls_in_messages(messages)

   # Tools
   tools = [
      write_todos,
      ls, read_file, write_file, edit_file, glob_files, grep_files, bash_command,
      delegate_research,  # Read-only sub-agent for codebase investigation
      # Agent file tools
      ls_agents, read_agent_file, write_agent_file, edit_agent_file,
      read_langgraph_json, edit_langgraph_json
   ]

   failed_tool_calls_count = state.get("failed_tool_calls_count", 0)
   if failed_tool_calls_count >= 3:
      messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user has had too many failed tool calls. DO NOT DO ANY NEW TOOL CALLS. Tell the user it's failed, and you need to stop and ask the user to try again in a different way. </NOTE_FROM_SYSTEM>")]
      # Don't bind tools when we've failed too many times - we want a text response only
      # invoke_with_cache owns the Anthropic-only cache_control decision. Raw node —
      # no middleware — so wrap the invoke in the shared rung-1 transient-error retry.
      response = invoke_with_transient_retry(
         lambda: invoke_with_cache(llm, messages, llm_model),
         label=f"rails_ai_builder_agent/{llm_model}",
      )
      # Reset counter by subtracting current count (since reducer uses operator.add)
      return {"messages": [response], "failed_tool_calls_count": -failed_tool_calls_count} # by adding a negative number, we subtract the current count and reset it to 0.

   # Bind tools - parallel_tool_calls is not supported by Gemini
   if llm_model.startswith("gemini"):
      llm_with_tools = llm.bind_tools(tools)
   else:
      llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)

   response = invoke_with_transient_retry(
      lambda: invoke_with_cache(llm_with_tools, messages, llm_model),
      label=f"rails_ai_builder_agent/{llm_model}",
   )
   return {"messages": [response]}

# Graph
def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)

    # Define nodes: these do the work
    builder.add_node("leonardo_ai_builder", leonardo_ai_builder)
    builder.add_node("tools", CappedToolNode(default_tools))
    
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