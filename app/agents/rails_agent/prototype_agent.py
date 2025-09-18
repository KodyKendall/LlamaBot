from langchain_openai import ChatOpenAI

from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph
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

@tool
def prototype_tool(state: RailsAgentState) -> str:
    """
    This tool is used to prototype the front-end UI/UX changes.
    """
    return "Prototype tool"

# Tools
tools = [prototype_tool]

# Node
def prototype_agent(state: RailsAgentState):
   llm = ChatOpenAI(model="gpt-4.1")

   messages = [sys_msg] + state["messages"]
   llm_with_tools = llm.bind_tools(tools)

   return {"messages": [llm_with_tools.invoke(messages)]}

# Graph
def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)

    # Define nodes: these do the work
    builder.add_node("prototype_agent", prototype_agent)
    builder.add_node("tools", ToolNode(tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "prototype_agent")
    builder.add_conditional_edges(
        "prototype_agent",
        # If the latest message (result) from prototype_agent is a tool call -> tools_condition routes to tools
        # If the latest message (result) from prototype_agent is a not a tool call -> tools_condition routes to END
        tools_condition,
    )
    builder.add_edge("tools", "prototype_agent")
    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph