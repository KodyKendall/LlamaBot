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
from app.agents.rails_agent.tools import write_todos, write_file, read_file, ls, edit_file, search_file, internet_search, bundle_exec_command, git_status, git_commit
from app.agents.rails_agent.prompts import RAILS_AGENT_PROMPT


# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Global tools list
tools = [write_todos, read_file, ls, internet_search, search_file, write_file, edit_file, bundle_exec_command, git_status, git_commit]

# System message
sys_msg = SystemMessage(content=RAILS_AGENT_PROMPT)

current_page_html = APP_DIR / 'page.html'
content = current_page_html.read_text()

# Node
def leonardo(state: RailsAgentState):
#    read_rails_file("app/agents/llamabot/nodes.py") # Testing.
   llm = ChatOpenAI(model="gpt-4.1")
   llm_with_tools = llm.bind_tools(tools)
   return {"messages": [llm_with_tools.invoke([sys_msg] + state["messages"])]}

def build_workflow(checkpointer=None):
    # Graph
    builder = StateGraph(RailsAgentState)

    # Define nodes: these do the work
    builder.add_node("leonardo", leonardo)
    builder.add_node("tools", ToolNode(tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "leonardo")
    builder.add_conditional_edges(
        "leonardo",
        # If the latest message (result) from leonardo is a tool call -> tools_condition routes to tools
        # If the latest message (result) from leonardo is a not a tool call -> tools_condition routes to END
        tools_condition,
    )
    builder.add_edge("tools", "leonardo")
    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph