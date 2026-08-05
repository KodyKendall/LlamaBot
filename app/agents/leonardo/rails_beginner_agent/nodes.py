from dotenv import load_dotenv
load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage

from langgraph.graph import START, StateGraph, END
from langgraph.types import Command, interrupt
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode
from langchain.tools import tool, ToolRuntime

from pathlib import Path
from typing import Literal

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, write_file, read_file, ls, edit_file, bash_command, tail_rails_logs, hard_restart_rails, fix_permissions,
    glob_files, grep_files, internet_search,
    read_leonardo_md, write_leonardo_md, edit_leonardo_md,
    read_brand_guide, write_brand_guide,
    save_memory, list_memories, delete_memory,
    build_use_skill_tool, list_skills, read_skill, write_skill, edit_skill, delete_skill,
    write_personality_file,
    browser_inspect, browser_inspect_enabled,
)
from app.agents.leonardo.rails_agent.sub_agents import delegate_task, delegate_research
from app.agents.leonardo.rails_beginner_agent.prompts import BEGINNER_AGENT_PROMPT
from app.agents.leonardo.project_context import build_beginner_system_prompt, brand_context_section
from app.agents.leonardo.friction import report_friction, with_friction_section
from app.agents.leonardo.llm_factory import get_llm
from app.agents.leonardo.agent_factory import repair_orphaned_tool_calls_in_messages
from app.agents.leonardo.resilience import invoke_with_transient_retry

import logging
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
APP_DIR = PROJECT_ROOT / 'app'


def get_sys_msg():
    # Rebuilt every turn (see get_sys_msg call site), so the brand guide appended
    # here stays live without a restart.
    full_prompt = with_friction_section(
        build_beginner_system_prompt(BEGINNER_AGENT_PROMPT, agent_mode="rails_beginner_agent")
        + brand_context_section()
    )
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


SUGGEST_PLAN_MODE_DESCRIPTION = """Suggest switching to Plan mode when the user's request is complex and would benefit from planning first.
Use this when:
- The request involves multiple pages or features
- The request needs clarification before building
- The request would take many steps to implement
- You're unsure what the user really wants

Parameters:
- reason: A short, friendly explanation of why Plan mode would help (in plain language, no tech jargon)

This tool will pause and show the user a button to switch to Plan mode. They can accept or decline."""


@tool(description=SUGGEST_PLAN_MODE_DESCRIPTION)
def suggest_plan_mode(
    reason: str,
    runtime: ToolRuntime,
) -> Command:
    """Suggest switching to Plan mode, pausing for user decision."""
    tool_call_id = runtime.tool_call_id

    # interrupt() freezes the agent. The frontend shows a switch/skip card.
    # When the user decides, interrupt() returns their answer.
    user_decision = interrupt({
        "type": "suggest_mode_switch",
        "target_mode": "plan",
        "reason": reason,
    })

    return Command(
        update={
            "messages": [ToolMessage(
                content=f"User responded to plan mode suggestion: {user_decision}",
                tool_call_id=tool_call_id
            )]
        }
    )


default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file, bash_command, tail_rails_logs, hard_restart_rails, fix_permissions,
    glob_files, grep_files, internet_search,
    read_leonardo_md, write_leonardo_md, edit_leonardo_md,
    read_brand_guide, write_brand_guide,  # Brand guide (colors, logos, notes)
    save_memory, list_memories, delete_memory,
    list_skills, read_skill, write_skill, edit_skill, delete_skill,  # Skill library management
    build_use_skill_tool(),  # use_skill — ToolNode execution (description is refreshed per-turn in leonardo_beginner)
    write_personality_file,
    delegate_task, delegate_research,
    suggest_plan_mode,
    report_friction,  # Papercut channel back to the LlamaPress team
    # browser_inspect is appended conditionally in build_workflow() — gated by the
    # `enable_browser_inspect` site setting (disabled by default).
]


def beginner_turn_tools(browser_inspect_on: bool = False) -> list:
    """The tools BOUND TO THE LLM for one beginner turn.

    Deliberately a second list from ``default_tools``, which only feeds the
    ToolNode: this one is rebuilt every turn so ``use_skill`` carries a fresh
    ``<available_skills>`` catalog (the raw StateGraph runs no middleware that
    could refresh it). Extracted from ``leonardo_beginner`` so the binding can be
    asserted in a unit test — a tool added to only one of the two lists is
    executable but invisible to the model, which is silent and hard to spot.

    NOTE (pre-existing drift, left alone here): this list is already a strict
    subset of ``default_tools`` — fix_permissions, the memory tools, and the
    brand-guide tools are registered on the ToolNode but never offered to the
    model in beginner mode.
    """
    tools = [
        write_todos,
        ls, read_file, write_file, edit_file, bash_command, tail_rails_logs, hard_restart_rails,
        glob_files, grep_files, internet_search,
        read_leonardo_md, write_leonardo_md, edit_leonardo_md,
        list_skills, read_skill, write_skill, edit_skill, delete_skill,
        build_use_skill_tool(),  # fresh <available_skills> catalog each turn (raw graph, no middleware)
        write_personality_file,
        delegate_task, delegate_research,
        suggest_plan_mode,
        report_friction,  # Papercut channel back to the LlamaPress team
    ]
    if browser_inspect_on:
        tools.append(browser_inspect)
    return tools


def leonardo_beginner(state: RailsAgentState, browser_inspect_on: bool = False) -> Command[Literal["tools"]]:
    llm_model = state.get('llm_model') or 'deepseek-v4-flash'
    logger.info(f"Using LLM model: {llm_model}")
    llm = get_llm(llm_model)

    view_path = (state.get('debug_info') or {}).get('view_path')

    messages = [get_sys_msg()] + state["messages"]
    if view_path:
        messages = messages + [HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user is currently viewing their Ruby on Rails webpage route at: " + view_path + " </NOTE_FROM_SYSTEM>"
        )]

    # Repair orphaned tool calls before EITHER .invoke() below. This raw
    # StateGraph node never runs AgentMiddleware, so it can't rely on
    # RepairOrphanedToolCallsMiddleware — without this an interrupted/crashed
    # tool call leaves a dangling AIMessage tool_call and every later turn 400s
    # ('insufficient tool messages'). SI#112. Only appends HumanMessages follow,
    # so one repair here covers the failure-limit and main invoke branches.
    messages = repair_orphaned_tool_calls_in_messages(messages)

    tools = beginner_turn_tools(browser_inspect_on)

    failed_tool_calls_count = state.get("failed_tool_calls_count", 0)
    if failed_tool_calls_count >= 3:
        messages = messages + [HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user has had too many failed tool calls. DO NOT DO ANY NEW TOOL CALLS. Tell the user it's failed in friendly, beginner-appropriate language, and suggest they try again with a simpler request. </NOTE_FROM_SYSTEM>"
        )]
        # Raw node — no DynamicModelMiddleware, so wrap the direct invoke in the
        # shared rung-1 retry (transient DeepSeek connection blips would otherwise
        # kill the turn with no retry at all).
        response = invoke_with_transient_retry(
            lambda: llm.invoke(messages),
            label=f"rails_beginner_agent/{llm_model}",
        )
        return {"messages": [response], "failed_tool_calls_count": -failed_tool_calls_count}

    if llm_model.startswith("gemini"):
        llm_with_tools = llm.bind_tools(tools)
    else:
        llm_with_tools = llm.bind_tools(tools, parallel_tool_calls=False)

    response = invoke_with_transient_retry(
        lambda: llm_with_tools.invoke(messages),
        label=f"rails_beginner_agent/{llm_model}",
    )
    return {"messages": [response]}


def build_workflow(checkpointer=None):
    from functools import partial

    builder = StateGraph(RailsAgentState)

    # Browser inspect (headless Chromium) is opt-in: only expose it when the
    # `enable_browser_inspect` site setting is on. Disabled by default. Read once
    # here so the node's bound tools and the ToolNode stay in sync.
    browser_inspect_on = browser_inspect_enabled()
    tool_list = list(default_tools)
    if browser_inspect_on:
        tool_list.append(browser_inspect)
        logger.info("browser_inspect tool enabled via site setting")

    builder.add_node("leonardo_beginner", partial(leonardo_beginner, browser_inspect_on=browser_inspect_on))
    builder.add_node("tools", ToolNode(tool_list))

    builder.add_edge(START, "leonardo_beginner")

    builder.add_conditional_edges(
        "leonardo_beginner",
        tools_condition,
        {"tools": "tools", END: END},
    )

    builder.add_edge("tools", "leonardo_beginner")

    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph
