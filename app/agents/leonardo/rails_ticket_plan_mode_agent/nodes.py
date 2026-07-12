"""
Rails Ticket Plan Mode Agent using LangChain 1.1+ create_agent with ToolRuntime.

Ticket Mode + a plan-first clarification step. Selected when Plan Mode is toggled ON
while the user is in Ticket Mode. Same ticket-writing machinery as Ticket Mode
(delegated research -> write_final_ticket -> offer_implementation), but it first grounds
the user-feedback story by asking clarifying questions one at a time — with live visual
options (ask_user_uiux_question) when the choice is about how something LOOKS.

This module stays DRY by reusing:
- Ticket Mode's ticket tools (write_final_ticket, offer_implementation) and sub-agents,
- Plan Mode's interaction tools (ask_user_question, ask_user_uiux_question) verbatim,
- Ticket Mode's middleware (view context, dynamic model, implementation-offer guarantee,
  failure circuit breaker),
with the only differences being the composed system prompt and the combined
ticket-plan-mode context middleware.

Features:
- Dynamic LLM model selection (defaults to Claude Haiku for efficiency)
- Automatic context summarization for long sessions
- View path context injection (via middleware)
- Failure circuit breaker after 3 failed tool calls
- ask_user_question / ask_user_uiux_question tools for plan-first grounding
- Deterministic implementation-offer guarantee after a ticket is created
- Anthropic prompt caching for reduced latency and costs
"""

from langchain_anthropic import ChatAnthropic
from app.agents.leonardo.agent_factory import build_leonardo_agent
from langchain_core.messages import SystemMessage
from datetime import date

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, write_file, edit_file, bash_command,
    fix_permissions,
    save_memory, list_memories, delete_memory,
    build_use_skill_tool, list_skills, read_skill, write_skill, edit_skill, delete_skill,
)
from app.agents.leonardo.rails_ticket_plan_mode_agent.prompts import TICKET_PLAN_MODE_AGENT_PROMPT
from app.agents.leonardo.project_context import build_system_prompt_with_project_context
from app.agents.leonardo.rails_ticket_plan_mode_agent.middleware import (
    inject_view_context,
    inject_ticket_plan_mode_context,
    check_failure_limit,
    ensure_implementation_offer,
    DynamicModelMiddleware,
)
from app.agents.leonardo.summarization import make_summarization_middleware
# Reuse Ticket Mode's ticket tools + research sub-agent verbatim (DRY).
from app.agents.leonardo.rails_ticket_mode_agent.nodes import (
    write_final_ticket,
    offer_implementation,
    SUMMARIZATION_PROMPT,
)
from app.agents.leonardo.rails_ticket_mode_agent.sub_agents import delegate_task
from app.agents.leonardo.rails_agent.sub_agents import delegate_research
# Reuse Plan Mode's interaction tools verbatim (DRY).
from app.agents.leonardo.rails_plan_mode_agent.nodes import (
    ask_user_question,
    ask_user_uiux_question,
)

import logging
logger = logging.getLogger(__name__)


def get_cached_system_prompt():
    """Build system message with project context, date, and prompt caching enabled."""
    current_date = date.today().strftime("%Y-%m-%d")
    date_suffix = f"\n\n---\n**Today's Date:** {current_date}"
    full_prompt = build_system_prompt_with_project_context(
        TICKET_PLAN_MODE_AGENT_PROMPT,
        suffix=date_suffix,
        agent_mode="rails_ticket_plan_mode_agent",
    )

    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": full_prompt,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    )


# Tool list - Ticket Mode's tools + the plan-first interaction tools (NO internet_search)
default_tools = [
    # Plan-first grounding (reused from rails_plan_mode_agent)
    ask_user_question,
    ask_user_uiux_question,
    # Standard tools
    write_todos,
    ls, read_file, write_file, edit_file,
    bash_command,
    fix_permissions,     # Fix permission issues in Rails container
    delegate_task,       # Sub-agent delegation for focused research tasks
    delegate_research,   # Read-only sub-agent for codebase investigation
    write_final_ticket,  # Creates ticket directly in Rails database
    offer_implementation,  # Offer to switch to engineer mode after ticket creation
    save_memory, list_memories, delete_memory,
    list_skills, read_skill, write_skill, edit_skill, delete_skill,  # Skill library
]


def build_workflow(checkpointer=None):
    """Build the Ticket Plan Mode agent workflow with create_agent.

    Args:
        checkpointer: Optional checkpointer for state persistence (e.g., PostgresSaver)

    Returns:
        A compiled LangGraph agent
    """
    # Default model (will be overridden by DynamicModelMiddleware based on state.llm_model)
    default_model = ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)

    # Configure middleware stack (order matters - executed top to bottom)
    middleware = [
        # 1. Summarization for long conversations
        make_summarization_middleware(summary_prompt=SUMMARIZATION_PROMPT),
        # 2. Dynamic model selection based on state.llm_model from frontend
        DynamicModelMiddleware(),
        # 3. View path context injection - prepends page context to user messages
        inject_view_context,
        # 4. Ticket plan mode context - write restrictions + ask-questions-first reminder
        inject_ticket_plan_mode_context,
        # 5. Deterministic implementation offer - guarantees the Yes/No interrupt fires
        #    after a ticket is created even if the model forgets the tool call.
        ensure_implementation_offer,
        # 6. Circuit breaker - stop tool calls after 3 failures
        check_failure_limit,
    ]

    # Create and return the agent
    return build_leonardo_agent(
        model=default_model,
        tools=[*default_tools, build_use_skill_tool()],
        system_prompt=get_cached_system_prompt(),
        state_schema=RailsAgentState,
        middleware=middleware,
        checkpointer=checkpointer,
    )
