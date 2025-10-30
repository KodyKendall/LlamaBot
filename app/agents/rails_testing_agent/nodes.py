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
    # Agent file tools
    ls_agents, read_agent_file, write_agent_file, edit_agent_file,
    read_langgraph_json, edit_langgraph_json
)
from app.agents.rails_testing_agent.prompts import RAILS_QA_SOFTWARE_ENGINEER_PROMPT

# from app.agents.rails_agent.prototype_agent.nodes import build_workflow as build_prototype_agent
# from app.agents.rails_agent.planning_agent import build_workflow as build_planning_agent

import logging
logger = logging.getLogger(__name__)


# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Global tools list

# System message
sys_msg = SystemMessage(content=RAILS_QA_SOFTWARE_ENGINEER_PROMPT)

current_page_html = APP_DIR / 'page.html'
content = current_page_html.read_text()

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
def leonardo_test_builder(state: RailsAgentState) -> Command[Literal["tools"]]:
   # ==================== LLM Model Selection ====================
   # Get model selection from state (passed from frontend)
   llm_model = state.get('llm_model', 'claude-4.5-haiku')
   logger.info(f"🤖 Using LLM model: {llm_model}")
   llm = get_llm(llm_model)
   # =============================================================

   view_path = (state.get('debug_info') or {}).get('view_path')

   show_full_html = False
   messages = [sys_msg] + state["messages"]
   
   if show_full_html:
      rendered_html = (state.get('debug_info') or {}).get('full_html')
      if rendered_html:
         messages = messages + [HumanMessage(content=(
            "<NOTE_FROM_SYSTEM> Below is the *rendered* HTML from the browser. "
            "This includes dynamic data and server-generated elements. "
            "Do NOT attempt to edit this literal HTML. "
            "Instead, use it to understand what the user sees visually. </NOTE_FROM_SYSTEM>\n\n"
            f"<RENDERED_HTML>{rendered_html}</RENDERED_HTML>"))]

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

   agent_mode = 'engineer' #state.get('agent_mode')
   if agent_mode:
        logger.info(f"🎯 User is in current mode: {agent_mode}")
        if agent_mode == 'prototype':
            messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user is in engineer mode. You are allowed to use the tools. Here are the tools you can use: tools = [write_todos, ls, read_file, write_file, edit_file, search_file, bash_command, internet_search, ls_agents, read_agent_file, write_agent_file, edit_agent_file, read_langgraph_json, edit_langgraph_json] </NOTE_FROM_SYSTEM>")]
            # return Command(goto="prototype_agent", update={})

        elif agent_mode == 'engineer': # just fall through here and let the tools_condition handle it
            messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user is in engineer mode. You are allowed to use the tools. Here are the tools you can use: tools = [write_todos, ls, read_file, write_file, edit_file, search_file, bash_command, internet_search, ls_agents, read_agent_file, write_agent_file, edit_agent_file, read_langgraph_json, edit_langgraph_json] </NOTE_FROM_SYSTEM>")]
        
      #   elif agent_mode == 'planning':
      #       return Command(goto="planning_agent", update={})
      #   elif agent_mode == 'ask': # just fall through here and let the tools_condition handle it
      #       tools = [ls, read_file, search_file, git_status, internet_search] 
      #       messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user is in ask mode. You are only allowed tools to read state, but not modify or do anything to the application. Here are the tools you can use: tools = [ls, read_file, search_file, git_status, internet_search] </NOTE_FROM_SYSTEM>")]

   failed_tool_calls_count = state.get("failed_tool_calls_count", 0)
   if failed_tool_calls_count >= 3:
      messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user has had too many failed tool calls. DO NOT DO ANY NEW TOOL CALLS. Tell the user it's failed, and you need to stop and ask the user to try again in a different way. </NOTE_FROM_SYSTEM>")]
      # Don't bind tools when we've failed too many times - we want a text response only
      response = llm.invoke(messages)
      # Reset counter by subtracting current count (since reducer uses operator.add)
      return {"messages": [response], "failed_tool_calls_count": -failed_tool_calls_count} # by adding a negative number, we subtract the current count and reset it to 0.

   messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user is in engineer mode. You are allowed to use the tools. Here are the tools you can use: tools = [write_todos, ls, read_file, write_file, edit_file, search_file, bash_command, internet_search, ls_agents, read_agent_file, write_agent_file, edit_agent_file, read_langgraph_json, edit_langgraph_json] </NOTE_FROM_SYSTEM>")]
   llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)
   response = llm_with_tools.invoke(messages)
   return {"messages": [response]}

# def should_continue(state: RailsAgentState) -> Literal["tools", "prototype_agent", END]:
#     last_message = state['messages'][-1]
#     if last_message.tool_calls:
#         return "tools"
#     if isinstance(last_message, Command):
#         if last_message.goto == "prototype_agent":
#             return "prototype_agent"
#         if last_message.goto == "planning_agent":
#             return "planning_agent"
#     return END

# Graph
def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)

    # Define nodes: these do the work
    builder.add_node("leonardo_test_builder", leonardo_test_builder)
    builder.add_node("tools", ToolNode(default_tools))
    
    # sub-agents:
    # builder.add_node("prototype_agent", build_prototype_agent(checkpointer=checkpointer))
   #  builder.add_node("planning_agent", build_planning_agent(checkpointer=checkpointer))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "leonardo_test_builder")

    builder.add_conditional_edges(
        "leonardo_test_builder",
        tools_condition,
        {"tools": "tools", END: END}, #"prototype_agent": "prototype_agent", END: END},
    )

    builder.add_edge("tools", "leonardo_test_builder")
    # builder.add_edge("prototype_agent", END)
   #  builder.add_edge("planning_agent", END)

    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph