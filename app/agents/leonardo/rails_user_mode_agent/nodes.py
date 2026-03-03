"""
Rails User Mode Agent (Database Mode) using LangChain 1.1+ create_agent with ToolRuntime.

This agent provides full ActiveRecord database access via bash_command:
- Execute ANY Rails console command
- Perform ALL ActiveRecord operations: CREATE, READ, UPDATE, DELETE
- Query models, run raw SQL, modify records
- CANNOT write code files, run git commands, or access system commands

Features:
- Dynamic LLM model selection (defaults to Claude Haiku for efficiency)
- Automatic context summarization for long sessions
- Failure circuit breaker after 3 failed tool calls
- ToolRuntime for state access in tools
- Anthropic prompt caching for reduced latency and costs
"""

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import SystemMessage
from datetime import date

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import bash_command
from app.agents.leonardo.rails_agent.sub_agents import delegate_research
from app.agents.leonardo.rails_user_mode_agent.prompts import USER_MODE_AGENT_PROMPT
from app.agents.leonardo.project_context import build_system_prompt_with_project_context
from app.agents.leonardo.rails_user_mode_agent.middleware import (
    inject_view_context,
    inject_database_mode_context,
    check_failure_limit,
    DynamicModelMiddleware,
)
from app.agents.utils.token_counter import gemini_multimodal_token_counter, SUMMARIZATION_TOKEN_THRESHOLD

import logging
logger = logging.getLogger(__name__)


# Summarization prompt for context extraction
SUMMARIZATION_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's database queries and operations.
This summary should capture the key information that would be essential for continuing to help the user without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts.

Your summary should include:

1. Primary Request: What is the user trying to accomplish with the database?
2. Key Queries: What queries were executed and what were the results?
3. Data Modified: Any records created, updated, or deleted
4. User Messages: All user messages (not tool results) - critical for understanding intent
5. Current Work: What were you working on immediately before this summary?
6. Next Step: If there's an obvious next step, note it

<example>
<analysis>
[Your thought process]
</analysis>

<summary>
1. Primary Request: User wants to clean up inactive user accounts

2. Key Queries:
   - Found 47 inactive users (last_login > 1 year ago)
   - Checked for associated orders (12 users have orders)

3. Data Modified:
   - Deleted 35 users without orders (IDs: 101-135)

4. User Messages:
   - "Find inactive users"
   - "Delete the ones without orders"

5. Current Work: Confirmed deletion of 35 inactive users

6. Next Step: User may want to handle the 12 users with orders
</summary>
</example>

# Conversation to summarize:
{messages}
"""


def get_cached_system_prompt():
    """Build system message with project context, date, and prompt caching enabled.

    Loads LEONARDO.md if it exists and appends it to the base prompt.
    Date is appended at the end so the main prompt can be cached.
    """
    current_date = date.today().strftime("%Y-%m-%d")
    date_suffix = f"\n\n---\n**Today's Date:** {current_date}"
    full_prompt = build_system_prompt_with_project_context(
        USER_MODE_AGENT_PROMPT,
        suffix=date_suffix
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
# Tool List - ONLY bash_command for database access
# =============================================================================

# Tools for database mode: bash_command + research delegation
default_tools = [
    bash_command,       # Rails console/runner for ActiveRecord operations
    delegate_research,  # Read-only sub-agent for codebase investigation
]


# =============================================================================
# Agent Builder
# =============================================================================

def build_workflow(checkpointer=None):
    """Build the User Mode (Database) agent workflow with create_agent.

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
    # Use Gemini 3 Flash for summarization (Google AI Studio, not Vertex)
    summarization_model = ChatGoogleGenerativeAI(
        model="gemini-3-flash-preview",
        vertexai=False,  # Explicitly use Google AI Studio, not Vertex AI
        temperature=1.0,
    )
    middleware = [
        # 1. Summarization for long conversations - prevents token limit issues
        SummarizationMiddleware(
            model=summarization_model,
            trigger=("tokens", SUMMARIZATION_TOKEN_THRESHOLD),
            keep=("messages", 20),  # Match Claude Code's default
            token_counter=gemini_multimodal_token_counter,
            trim_tokens_to_summarize=None,  # Let Gemini see everything
            summary_prompt=SUMMARIZATION_PROMPT,
        ),
        # 2. Dynamic model selection based on state.llm_model from frontend
        DynamicModelMiddleware(),
        # 3. View path context injection - prepends page context to user messages
        inject_view_context,
        # 4. Database mode context - reminds agent of database-only restrictions
        inject_database_mode_context,
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
