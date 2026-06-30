"""
Rails Engineer Plan Mode Agent using LangChain 1.1+ create_agent with ToolRuntime.

The engineering counterpart to Beginner Plan Mode. Same plan-first 6-phase workflow
(Clarify → Research → Refine → Present Plan → Implement → Verify) driven by the
`ask_user_question` one-question-at-a-time mechanism, but retains the full engineering
depth of Engineer Mode (its playbook is composed into ENGINEER_PLAN_PROMPT).

This module reuses the plan-mode tooling (`ask_user_question`, `ask_user_uiux_question`)
and summarization prompt verbatim to stay DRY — the only thing that differs from
rails_plan_mode_agent is the system prompt and the mode-context middleware.

Features:
- Dynamic LLM model selection (defaults to DeepSeek V4 Flash)
- Automatic context summarization for long sessions
- View path context injection (via middleware)
- Failure circuit breaker after 3 failed tool calls
- ask_user_question / ask_user_uiux_question tools for structured user interaction
- Anthropic prompt caching for reduced latency and costs
"""

from langchain_anthropic import ChatAnthropic
from app.agents.leonardo.llm_factory import make_summarization_model
from app.agents.leonardo.agent_factory import build_leonardo_agent
from langchain.agents.middleware import SummarizationMiddleware
from app.agents.leonardo.summarization import make_summarization_middleware
from langchain_core.messages import SystemMessage
from datetime import date

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, write_file, edit_file, search_file, bash_command,
    tail_rails_logs, hard_restart_rails,
    glob_files, grep_files, internet_search,
    read_leonardo_md, write_leonardo_md, edit_leonardo_md,
    save_memory, list_memories, delete_memory,
    write_personality_file,
)
from app.agents.leonardo.rails_engineer_plan_mode_agent.prompts import ENGINEER_PLAN_PROMPT
from app.agents.leonardo.project_context import build_beginner_system_prompt
from app.agents.leonardo.rails_engineer_plan_mode_agent.middleware import (
    inject_view_context,
    inject_engineer_plan_mode_context,
    check_failure_limit,
    DynamicModelMiddleware,
)
from app.agents.utils.token_counter import SUMMARIZATION_TOKEN_THRESHOLD
from app.agents.leonardo.rails_agent.sub_agents import delegate_task, delegate_research
# Reuse the plan-mode interaction tools + summarization prompt verbatim (DRY).
from app.agents.leonardo.rails_plan_mode_agent.nodes import (
    ask_user_question,
    ask_user_uiux_question,
    SUMMARIZATION_PROMPT,
)

import logging
logger = logging.getLogger(__name__)


def get_cached_system_prompt():
    """Build system message with project context, personality files, date, and prompt caching."""
    current_date = date.today().strftime("%Y-%m-%d")
    date_suffix = f"\n\n---\n**Today's Date:** {current_date}"
    full_prompt = build_beginner_system_prompt(
        ENGINEER_PLAN_PROMPT,
        suffix=date_suffix,
        agent_mode="rails_engineer_plan_mode_agent",
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


# =============================================================================
# Tool list and workflow
# =============================================================================

default_tools = [
    # Plan mode specific (reused from rails_plan_mode_agent)
    ask_user_question,
    ask_user_uiux_question,
    # Standard tools (same as engineer + search_file)
    write_todos,
    ls, read_file, write_file, edit_file, search_file, bash_command,
    tail_rails_logs, hard_restart_rails,
    glob_files, grep_files, internet_search,
    read_leonardo_md, write_leonardo_md, edit_leonardo_md,
    save_memory, list_memories, delete_memory,
    write_personality_file,
    # Sub-agent delegation
    delegate_task, delegate_research,
]


def build_workflow(checkpointer=None):
    """Build the Engineer Plan Mode agent workflow with create_agent.

    Args:
        checkpointer: Optional checkpointer for state persistence (e.g., PostgresSaver)

    Returns:
        A compiled LangGraph agent
    """
    # Default model (will be overridden by DynamicModelMiddleware based on state.llm_model)
    default_model = ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)

    # Configure middleware stack (order matters - executed top to bottom)
    middleware = [
        # 1. Summarization for long conversations (shared factory: provider
        #    fallback model, token-budgeted keep, first-user-messages + todo
        #    preservation; REMOVE_ALL honored by the DeltaChannel reducer).
        make_summarization_middleware(summary_prompt=SUMMARIZATION_PROMPT),
        # 2. Dynamic model selection based on state.llm_model from frontend
        DynamicModelMiddleware(),
        # 3. View path context injection
        inject_view_context,
        # 4. Engineer plan mode context - reminds agent of phase workflow
        inject_engineer_plan_mode_context,
        # 5. Circuit breaker - stop tool calls after 3 failures
        check_failure_limit,
    ]

    # Create and return the agent
    return build_leonardo_agent(
        model=default_model,
        tools=default_tools,
        system_prompt=get_cached_system_prompt(),
        state_schema=RailsAgentState,
        middleware=middleware,
        checkpointer=checkpointer,
    )
