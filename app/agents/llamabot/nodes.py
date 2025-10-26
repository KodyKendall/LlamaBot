from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.chat_agent_executor import AgentState


import asyncio
from pathlib import Path
import os

from openai import OpenAI
from app.agents.utils.images import encode_image

# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Global tools list
tools = []

# System message
sys_msg = "You are a helpful assistant. Your favorite animal is cyborg llama."

# ====================================================================================
# SINGLETON LLM INSTANCE - Created once at module load, reused across all requests
# This eliminates duplicate httpx connection pools and reduces memory usage
# LangChain chat models are thread-safe and designed for this pattern
# ====================================================================================
_llm_instance = ChatOpenAI(model="gpt-4.1")

# Warning: Brittle - None type will break this when it's injected into the state for the tool call, and it silently fails. So if it doesn't map state types properly from the frontend, it will break. (must be exactly what's defined here).
class LlamaPressState(AgentState):
    api_token: str
    agent_prompt: str

# Node
def leo(state: LlamaPressState):
#    read_rails_file("app/agents/llamabot/nodes.py") # Testing.
   # Reuse singleton LLM instance (memory efficient, thread-safe)
   llm_with_tools = _llm_instance.bind_tools(tools)

   custom_prompt_instructions_from_llamapress_dev = state.get("agent_prompt")
   full_sys_msg = SystemMessage(content=f"""{sys_msg} Here are additional instructions provided by the developer: <DEVELOPER_INSTRUCTIONS> {custom_prompt_instructions_from_llamapress_dev} </DEVELOPER_INSTRUCTIONS>""")

   return {"messages": [llm_with_tools.invoke([full_sys_msg] + state["messages"])]}

def build_workflow(checkpointer=None):
    # Graph
    builder = StateGraph(LlamaPressState)

    # Define nodes: these do the work
    builder.add_node("leo", leo)
    builder.add_node("tools", ToolNode(tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "leo")
    builder.add_conditional_edges(
        "leo",
        # If the latest message (result) from leo is a tool call -> tools_condition routes to tools
        # If the latest message (result) from leo is a not a tool call -> tools_condition routes to END
        tools_condition,
    )
    builder.add_edge("tools", "leo")
    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph