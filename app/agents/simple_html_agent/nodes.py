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

from app.agents.utils.playwright_screenshot import capture_page_and_img_src

from openai import OpenAI
from app.agents.utils.images import encode_image

# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Single HTML output file
HTML_OUTPUT_PATH = APP_DIR / 'page.html'

@tool
def write_html_page(full_html_document: str) -> str:
    """
    Write HTML code to a file.
    """
    with open(HTML_OUTPUT_PATH, "w", encoding='utf-8') as f:
        f.write(full_html_document)
    return f"HTML code written to {HTML_OUTPUT_PATH.relative_to(PROJECT_ROOT)}"

# Global tools list
tools = [write_html_page]

current_page_html = APP_DIR / 'page.html'
content = current_page_html.read_text()

# System message
sys_msg = SystemMessage(content=f"""You are a helpful assistant that can write HTML code to a file. Here is the current HTML code:
{content}

You can use the write_html_page tool to write HTML code to the file.

You can use the read_html_page tool to read the HTML code from the file.

""")

# Node
def leonardo(state: MessagesState):
#    read_rails_file("app/agents/llamabot/nodes.py") # Testing.
   llm = ChatOpenAI(model="gpt-4.1")
   llm_with_tools = llm.bind_tools(tools)
   return {"messages": [llm_with_tools.invoke([sys_msg] + state["messages"])]}

def build_workflow(checkpointer=None):
    # Graph
    builder = StateGraph(MessagesState)

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