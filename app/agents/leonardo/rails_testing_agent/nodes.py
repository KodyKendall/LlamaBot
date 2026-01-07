"""
Rails Testing Agent using LangChain 1.1+ create_agent with ToolRuntime.

This agent specializes in TDD bug reproduction:
- Stage 1: Bug Intake - Gather structured bug report from user
- Stage 2: Test Design - Determine test type (model/request/feature spec)
- Stage 3: Failing Test - Write test that proves bug exists (RED)
- Stage 4: Verification - Run test to confirm failure
- Stage 5: Hand-off - Ready for fix (user switches to Engineer mode)
- Stage 6: Regression - After fix, test should pass (GREEN)

Features:
- Dynamic LLM model selection (defaults to Claude Haiku for efficiency)
- Automatic context summarization for long sessions
- View path context injection (via middleware)
- Failure circuit breaker after 3 failed tool calls
- ToolRuntime for state access in tools
- Anthropic prompt caching for reduced latency and costs

Note: We use langchain.agents.create_agent with ToolRuntime pattern instead of
langgraph's InjectedState because create_agent provides middleware support.
"""

from langchain_anthropic import ChatAnthropic
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import SystemMessage
from datetime import date

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, write_file, edit_file, search_file, bash_command,
    # Agent file tools (for reading test patterns from other agents)
    ls_agents, read_agent_file, write_agent_file, edit_agent_file,
    read_langgraph_json, edit_langgraph_json
)
from app.agents.leonardo.rails_testing_agent.prompts import RAILS_TESTING_AGENT_PROMPT
from app.agents.leonardo.rails_testing_agent.middleware import (
    inject_view_context,
    inject_testing_mode_context,
    check_failure_limit,
    DynamicModelMiddleware,
)

import logging
logger = logging.getLogger(__name__)


def get_cached_system_prompt():
    """Build system message with current date and prompt caching enabled.

    Date is appended at the end so the main prompt can be cached,
    and only the date portion changes daily.
    """
    current_date = date.today().strftime("%Y-%m-%d")
    prompt_with_date = f"{RAILS_TESTING_AGENT_PROMPT}\n\n---\n**Today's Date:** {current_date}\n(Use this date for regression test naming: bug_{current_date}_description_spec.rb)"

    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": prompt_with_date,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    )


# Tool list - tools available to the Testing agent
default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file, search_file,
    bash_command,  # For running rspec tests
    # Agent file tools (for reference/reading patterns)
    ls_agents, read_agent_file, write_agent_file, edit_agent_file,
    read_langgraph_json, edit_langgraph_json
]


def build_workflow(checkpointer=None):
    """Build the Testing agent workflow with create_agent.

    Args:
        checkpointer: Optional checkpointer for state persistence (e.g., PostgresSaver)

    Returns:
        A compiled LangGraph agent

    Note: Uses SystemMessage with cache_control for Anthropic prompt caching.
    This requires LangChain 1.1.0+ which added SystemMessage support to create_agent.
    The system prompt is cached for 5 minutes, reducing input token costs by ~90%.
    """
    # Default model (will be overridden by DynamicModelMiddleware based on state.llm_model)
    default_model = ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)

    # Configure middleware stack (order matters - executed top to bottom)
    middleware = [
        # 1. Summarization for long conversations - prevents token limit issues
        SummarizationMiddleware(
            model="claude-haiku-4-5",
            max_tokens_before_summary=80000,
            messages_to_keep=40,
        ),
        # 2. Dynamic model selection based on state.llm_model from frontend
        DynamicModelMiddleware(),
        # 3. View path context injection - prepends page context to user messages
        inject_view_context,
        # 4. Testing mode context - reminds agent of TDD bug reproduction workflow
        inject_testing_mode_context,
        # 5. Circuit breaker - stop tool calls after 3 failures
        check_failure_limit,
    ]

    # Create and return the agent
    return create_agent(
        model=default_model,
        tools=default_tools,
        system_prompt=get_cached_system_prompt(),
        state_schema=RailsAgentState,
        middleware=middleware,
        checkpointer=checkpointer,
    )
