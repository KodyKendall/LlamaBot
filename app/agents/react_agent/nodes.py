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

# System message
sys_msg = SystemMessage(content="""You are a helpful software_developer_assistant tasked with writing HTML, CSS, and JavaScript code. 
Everything should be written to a SINGLE HTML file located at app/page.html. 
CSS should be included in a <style> tag in the <head> section.
JavaScript should be included in a <script> tag at the end of the <body> section.
Do NOT create separate CSS or JS files - everything goes in the single HTML document.
If you are cloning a webpage, you will write the final output directly into this single HTML file.""")

# Node
def software_developer_assistant(state: MessagesState):
   llm = ChatOpenAI(model="o4-mini")
   llm_with_tools = llm.bind_tools(tools)
   return {"messages": [llm_with_tools.invoke([sys_msg] + state["messages"])]}

def build_workflow(checkpointer=None):
    # Graph
    builder = StateGraph(MessagesState)

    # Define nodes: these do the work
    builder.add_node("software_developer_assistant", software_developer_assistant)
    builder.add_node("tools", ToolNode(tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "software_developer_assistant")
    builder.add_conditional_edges(
        "software_developer_assistant",
        # If the latest message (result) from software_developer_assistant is a tool call -> tools_condition routes to tools
        # If the latest message (result) from software_developer_assistant is a not a tool call -> tools_condition routes to END
        tools_condition,
    )
    builder.add_edge("tools", "software_developer_assistant")
    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph