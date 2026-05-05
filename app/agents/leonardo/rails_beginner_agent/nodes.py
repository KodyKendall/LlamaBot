from langchain_core.tools import tool
from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph, END
from langgraph.types import Command
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode

from pathlib import Path
from typing import Literal

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, write_file, read_file, ls, edit_file, bash_command,
    glob_files, grep_files, internet_search,
    read_leonardo_md, write_leonardo_md, edit_leonardo_md,
    save_memory, list_memories, delete_memory,
    write_personality_file,
)
from app.agents.leonardo.rails_agent.sub_agents import delegate_task, delegate_research
from app.agents.leonardo.rails_beginner_agent.prompts import BEGINNER_AGENT_PROMPT
from app.agents.leonardo.project_context import build_beginner_system_prompt
from app.agents.leonardo.llm_factory import get_llm

import logging
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
APP_DIR = PROJECT_ROOT / 'app'


def get_sys_msg():
    full_prompt = build_beginner_system_prompt(BEGINNER_AGENT_PROMPT)
    return {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": full_prompt,
                "cache_control": {"type": "ephemeral"},
            },
        ],
    }


default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file, bash_command,
    glob_files, grep_files, internet_search,
    read_leonardo_md, write_leonardo_md, edit_leonardo_md,
    save_memory, list_memories, delete_memory,
    write_personality_file,
    delegate_task, delegate_research,
]


def leonardo_beginner(state: RailsAgentState) -> Command[Literal["tools"]]:
    llm_model = state.get('llm_model') or 'deepseek-v4-flash'
    logger.info(f"Using LLM model: {llm_model}")
    llm = get_llm(llm_model)

    view_path = (state.get('debug_info') or {}).get('view_path')

    messages = [get_sys_msg()] + state["messages"]
    if view_path:
        messages = messages + [HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user is currently viewing their Ruby on Rails webpage route at: " + view_path + " </NOTE_FROM_SYSTEM>"
        )]

    tools = [
        write_todos,
        ls, read_file, write_file, edit_file, bash_command,
        glob_files, grep_files, internet_search,
        read_leonardo_md, write_leonardo_md, edit_leonardo_md,
        write_personality_file,
        delegate_task, delegate_research,
    ]

    failed_tool_calls_count = state.get("failed_tool_calls_count", 0)
    if failed_tool_calls_count >= 3:
        messages = messages + [HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user has had too many failed tool calls. DO NOT DO ANY NEW TOOL CALLS. Tell the user it's failed in friendly, beginner-appropriate language, and suggest they try again with a simpler request. </NOTE_FROM_SYSTEM>"
        )]
        response = llm.invoke(messages)
        return {"messages": [response], "failed_tool_calls_count": -failed_tool_calls_count}

    if llm_model.startswith("gemini"):
        llm_with_tools = llm.bind_tools(tools)
    else:
        llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)

    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)

    builder.add_node("leonardo_beginner", leonardo_beginner)
    builder.add_node("tools", ToolNode(default_tools))

    builder.add_edge(START, "leonardo_beginner")

    builder.add_conditional_edges(
        "leonardo_beginner",
        tools_condition,
        {"tools": "tools", END: END},
    )

    builder.add_edge("tools", "leonardo_beginner")

    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph
