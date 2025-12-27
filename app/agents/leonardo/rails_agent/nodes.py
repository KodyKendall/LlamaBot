"""
Rails Agent using LangChain 1.1+ create_agent with ToolRuntime.

This agent helps non-technical users build Ruby on Rails applications with:
- Dynamic LLM model selection (Claude Haiku, Sonnet, GPT-5 Codex)
- Automatic context summarization for long sessions
- View path context injection
- Failure circuit breaker after 3 failed tool calls
- ToolRuntime for state access in tools
- Anthropic prompt caching for reduced latency and costs (via SystemMessage with cache_control)

Note: We use langchain.agents.create_agent with ToolRuntime pattern instead of
langgraph's InjectedState because create_agent provides middleware support.
"""

from langchain_anthropic import ChatAnthropic
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import SystemMessage

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, write_file, edit_file,
    search_file, bash_command, git_status, git_commit,
    git_command, github_cli_command, internet_search
)
from app.agents.leonardo.rails_agent.prompts import RAILS_AGENT_PROMPT
from app.agents.leonardo.rails_agent.middleware import (
    inject_view_context,
    check_failure_limit,
    DynamicModelMiddleware,
)
from app.agents.leonardo.rails_agent.sub_agents import delegate_task

import logging
logger = logging.getLogger(__name__)

# System message with Anthropic prompt caching enabled
# This caches the ~4000 token system prompt for 5 minutes, reducing input token costs by ~90%
CACHED_SYSTEM_PROMPT = SystemMessage(
    content=[
        {
            "type": "text",
            "text": RAILS_AGENT_PROMPT,
            "cache_control": {"type": "ephemeral"}
        }
    ]
)

# Tool list - all tools available to the Rails agent
default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file, search_file,
    bash_command,
    # git_status, git_commit, 
    # git_command, 
    # github_cli_command,
    internet_search,
    delegate_task,  # Sub-agent delegation for specialized tasks
]

def build_workflow(checkpointer=None):
    """Build the Rails agent workflow with create_agent and middleware.

    Args:
        checkpointer: Optional checkpointer for state persistence (e.g., PostgresSaver)

    Returns:
        A compiled LangGraph agent with middleware support and ToolRuntime

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
            model="claude-sonnet-4-5",
            max_tokens_before_summary=80000,  # Trigger summarization at 80k tokens
            messages_to_keep=40,              # Keep last 40 messages after summary
        ),
        # 2. Dynamic model selection based on state.llm_model from frontend
        DynamicModelMiddleware(),
        # 3. Inject view path context when user is viewing a specific page
        inject_view_context,
        # 4. Circuit breaker - stop tool calls after 3 failures
        check_failure_limit,
    ]

    # Create agent with middleware - uses ToolRuntime for state access in tools
    # CACHED_SYSTEM_PROMPT enables Anthropic prompt caching via cache_control
    agent = create_agent(
        model=default_model,
        tools=default_tools,
        system_prompt=CACHED_SYSTEM_PROMPT,  # SystemMessage with cache_control
        state_schema=RailsAgentState,
        middleware=middleware,
        checkpointer=checkpointer,
    )

    return agent
