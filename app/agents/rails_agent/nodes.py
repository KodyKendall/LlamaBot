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

# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

@tool
def read_rails_file(filepath: str) -> str:
    """Read the contents of a Rails file."""
    # Construct the full path
    full_path = APP_DIR / "rails" / filepath
    
    # Check if file exists
    if not full_path.exists():
        return f"File not found: {filepath}"
    
    # Read the file contents
    try:
        # Option 1: Using pathlib (recommended)
        contents = full_path.read_text()
        
        # Option 2: Using traditional open()
        # with open(full_path, 'r') as f:
        #     contents = f.read()
        
        return contents
    except Exception as e:
        return f"Error reading file: {e}"

@tool 
def list_directory_contents(directory: str = "") -> str:
    """
    List the contents of a directory.
    If directory is empty, lists the rails root directory.
    """
    # Build path - if directory is empty, just use rails root
    dir_path = APP_DIR / "rails" / directory if directory else APP_DIR / "rails"
    
    if not dir_path.exists():
        return f"Directory not found: {dir_path}"
    
    return os.listdir(dir_path)

@tool 
def write_to_file(filepath: str, content: str) -> str:
    """
    Write content to a file.
    """
    full_path = APP_DIR / "rails" / filepath
    if not full_path.exists():
        return f"File not found: {filepath}"

    with open(full_path, "w", encoding='utf-8') as f:
        f.write(content)

    return f"Content written to {full_path}"

# Global tools list
tools = [list_directory_contents, read_rails_file, write_to_file]

# System message
sys_msg = SystemMessage(content=f"""You are Leonardo, 
a Llama that can read and write changes to a Ruby on Rails application.
Your task is to help the user with their Ruby on Rails application, 
by answering questions, making modifications, etc.
You can list the contents of the Rails directory to explore the app.
You can read the contents of existing files in the Rails directory, to understand the app.
You can write content to existing files in the Rails directory, to make changes to the app.
""")

current_page_html = APP_DIR / 'page.html'
content = current_page_html.read_text()

class Todo(TypedDict):
    task: str
    status: Literal["pending", "in_progress", "completed"]

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