from langchain_openai import ChatOpenAI

from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph, END
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
from app.agents.rails_agent.tools import write_todos, write_file, read_file, ls, edit_file, search_file, internet_search, bash_command, git_status, git_commit, git_command, view_page, github_cli_command
from app.agents.rails_agent.prompts import RAILS_AGENT_PROMPT

from app.agents.rails_agent.prototype_agent.prototype_agent import build_workflow as build_prototype_agent
from app.agents.rails_agent.planning_agent import build_workflow as build_planning_agent

import logging
logger = logging.getLogger(__name__)


# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Global tools list

# System message
sys_msg = SystemMessage(content=RAILS_AGENT_PROMPT)

current_page_html = APP_DIR / 'page.html'
content = current_page_html.read_text()

default_tools = [write_todos,
         ls, read_file, write_file, edit_file, search_file, bash_command, 
         git_status, git_commit, git_command, github_cli_command, internet_search]

     #     view_page, /
# Node
def leonardo(state: RailsAgentState):
   llm = ChatOpenAI(model="gpt-4.1")

# gemini_api_key = os.getenv("GEMINI_API_KEY")
#    llm = ChatGemini(
#     model="gemini-2.5-pro",
#     temperature=1.0,
#     max_retries=2,
#     google_api_key=gemini_api_key)

   view_path = (state.get('debug_info') or {}).get('view_path')

   show_full_html = True
   messages = [sys_msg] + state["messages"]

   if view_path:
        messages = messages + [HumanMessage(content="NOTE FROM SYSTEM: The user is currently viewing their Ruby on Rails webpage route at: " + view_path)]
   
   if show_full_html:
        full_html = (state.get('debug_info') or {}).get('full_html')
        if full_html:
            messages = messages + [HumanMessage(content="NOTE FROM SYSTEM: Here's the full HTML of the page they're viewing: " + full_html)]

      # Tools
   tools = [write_todos,
         ls, read_file, write_file, edit_file, search_file, bash_command, 
         git_status, git_commit, git_command, github_cli_command, internet_search]

   agent_mode = state.get('agent_mode')
   if agent_mode:
        logger.info(f"🎯 User is in current mode: {agent_mode}")
        if agent_mode == 'prototype':
            return {"next": "prototype_agent"}
        elif agent_mode == 'engineer':
            messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user is in engineer mode. You are allowed to use the tools. Here are the tools you can use: tools = [write_todos, ls, read_file, write_file, edit_file, search_file, bash_command, git_status, git_commit, git_command, github_cli_command, internet_search] </NOTE_FROM_SYSTEM>")]
        elif agent_mode == 'ask':
            tools = [ls, read_file, search_file, git_status, internet_search] 
            messages = messages + [HumanMessage(content="<NOTE_FROM_SYSTEM> The user is in ask mode. You are only allowed tools to read state, but not modify or do anything to the application. Here are the tools you can use: tools = [ls, read_file, search_file, git_status, internet_search] </NOTE_FROM_SYSTEM>")]

   llm_with_tools = llm.bind_tools(tools)

   return {"messages": [llm_with_tools.invoke(messages)]}

def leonardo_router(state: RailsAgentState) -> Literal["tools", "prototype_agent", "planning_agent", "__end__"]:
    if next_node := state.get("next"):
        return next_node
    if tools_condition(state) == "tools":
        return "tools"
    return END

# Graph
def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)

    # Define nodes: these do the work
    builder.add_node("leonardo", leonardo)
    builder.add_node("tools", ToolNode(default_tools))
    
    # sub-agents:
    builder.add_node("prototype_agent", build_prototype_agent(checkpointer=checkpointer))
    builder.add_node("planning_agent", build_planning_agent(checkpointer=checkpointer))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "leonardo")
    builder.add_conditional_edges(
        "leonardo",
        leonardo_router,
        {
            "tools": "tools",
            "prototype_agent": "prototype_agent",
            "planning_agent": "planning_agent",
            END: END,
        },
    )
    builder.add_edge("tools", "leonardo")
    builder.add_edge("prototype_agent", END)
    builder.add_edge("planning_agent", END)

    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph