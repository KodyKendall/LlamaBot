"""
Rails User Feedback Agent v2 using LangChain 1.1+ create_agent with ToolRuntime.

This agent helps users understand their Rails app and captures feedback:
- READ any file in the codebase
- QUERY the database via Rails console (SELECT-like only)
- CREATE feedback entries in LlamaBotRails::UserFeedback
- DELEGATE complex research to sub-agents
- CANNOT write code, run git commands, or make destructive changes

Features:
- Dynamic LLM model selection (defaults to Claude Haiku for efficiency)
- Automatic context summarization for long sessions
- View path context injection (via middleware)
- Failure circuit breaker after 3 failed tool calls
- ToolRuntime for state access in tools
- Anthropic prompt caching for reduced latency and costs
"""

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import SystemMessage, ToolMessage
from langchain.tools import tool, ToolRuntime
from langgraph.types import Command
from datetime import date
import base64

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, search_file, bash_command,
    glob_files, grep_files, rails_api_sh,
)
from app.agents.leonardo.rails_user_feedback_agent.prompts import USER_FEEDBACK_AGENT_PROMPT
from app.agents.leonardo.project_context import build_system_prompt_with_project_context
from app.agents.leonardo.rails_user_feedback_agent.middleware import (
    inject_view_context,
    inject_user_mode_context,
    check_failure_limit,
    DynamicModelMiddleware,
)
from app.agents.leonardo.rails_user_feedback_agent.sub_agents import delegate_task
from app.agents.utils.token_counter import gemini_multimodal_token_counter, SUMMARIZATION_TOKEN_THRESHOLD

import logging
logger = logging.getLogger(__name__)

# Summarization prompt for context extraction
SUMMARIZATION_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's questions and your previous research.
This summary should capture the key information that would be essential for continuing to help the user without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts.

Your summary should include:

1. Primary Request: What is the user trying to understand or accomplish?
2. Key Findings: What did you learn from reading files or querying the database?
3. Files Examined: Which files were read and what was important in each?
4. Feedback Captured: Any feedback entries that were drafted or saved
5. User Messages: All user messages (not tool results) - critical for understanding intent
6. Current Work: What were you working on immediately before this summary?
7. Next Step: If there's an obvious next step, note it

<example>
<analysis>
[Your thought process]
</analysis>

<summary>
1. Primary Request: User wants to understand how order totals are calculated

2. Key Findings:
   - Order model has `calculate_total` method
   - Total is sum of line_item amounts
   - Discount applied via callback

3. Files Examined:
   - rails/db/schema.rb - Found orders and line_items tables
   - rails/app/models/order.rb - Found calculation logic

4. Feedback Captured: None yet

5. User Messages:
   - "How does the pricing work?"
   - "What about discounts?"

6. Current Work: Explaining the discount callback behavior

7. Next Step: Answer any follow-up questions about the calculation
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
        USER_FEEDBACK_AGENT_PROMPT,
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
# write_feedback Tool
# =============================================================================

WRITE_FEEDBACK_DESCRIPTION = """Create a feedback entry in the Rails database.

Parameters:
- title: Feedback title (e.g., "Bug: Total shows 0 on Order page", "Suggestion: Add CSV export")
- description: The observation template content (URL, User Story, Current/Desired Behavior, Verification Criteria)
- feedback_type: One of: bug, suggestion, question, complaint, praise, general
- priority: 0=Low, 1=Medium (default), 2=High, 3=Critical

Returns confirmation with the created feedback ID.

Use this tool to persist user feedback to the database. Always draft the observation first,
get user confirmation if possible, then call this tool to save it."""


@tool(description=WRITE_FEEDBACK_DESCRIPTION)
def write_feedback(
    title: str,
    description: str,
    feedback_type: str,
    runtime: ToolRuntime,
    priority: int = 1,
) -> Command:
    """Create a feedback entry directly in the Rails database."""
    tool_call_id = runtime.tool_call_id

    # Validate feedback_type
    valid_types = ['bug', 'suggestion', 'question', 'complaint', 'praise', 'general']
    if feedback_type not in valid_types:
        feedback_type = 'general'

    # Validate priority
    if priority not in [0, 1, 2, 3]:
        priority = 1

    # Base64 encode text fields to avoid shell/Ruby escaping issues
    def b64(s: str) -> str:
        return base64.b64encode(s.encode('utf-8')).decode('ascii')

    title_b64 = b64(title)
    description_b64 = b64(description)

    # Ruby code that decodes base64 and creates the feedback
    # Note: user_id is set to 1 (system user) and user_email indicates Leonardo created it
    ruby_script = f'''
require "base64"
feedback = LlamaBotRails::UserFeedback.create!(
  title: Base64.decode64("{title_b64}").force_encoding("UTF-8"),
  description: Base64.decode64("{description_b64}").force_encoding("UTF-8"),
  feedback_type: "{feedback_type}",
  priority: {priority},
  status: "open",
  user_id: 1,
  user_email: "leonardo-user-mode@llamabot.ai"
)
puts "FEEDBACK_CREATED:" + feedback.id.to_s
'''

    # Execute via rails_api_sh
    command = f"bin/rails runner '{ruby_script}'"
    result = rails_api_sh(command)

    # Parse result
    if "FEEDBACK_CREATED:" in result:
        feedback_id = result.split("FEEDBACK_CREATED:")[1].strip().split()[0]
        success_msg = f"Feedback saved successfully with ID: {feedback_id}"
        tool_output = {"status": "success", "feedback_id": feedback_id}
        return Command(
            update={
                "messages": [ToolMessage(success_msg, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )
    else:
        error_msg = f"Failed to create feedback: {result}"
        tool_output = {"status": "error", "message": result}
        return Command(
            update={
                "messages": [ToolMessage(error_msg, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )


# =============================================================================
# Tool List - READ-ONLY + feedback + delegation
# =============================================================================

# Tools available to the User Feedback Agent v2
# NO write_file, edit_file, or agent file tools
default_tools = [
    write_todos,       # Task tracking for complex research
    ls,                # List directories
    read_file,         # Read any file
    search_file,       # Search for substring in files
    glob_files,        # Find files by pattern
    grep_files,        # Regex search in files
    bash_command,      # Rails console queries (SELECT only - enforced by prompt)
    write_feedback,    # Save feedback to database
    delegate_task,     # Spawn research sub-agent for complex questions
]


# =============================================================================
# Agent Builder
# =============================================================================

def build_workflow(checkpointer=None):
    """Build the User Feedback agent workflow with create_agent.

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
        # 4. User mode context - reminds agent of read-only restrictions
        inject_user_mode_context,
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
