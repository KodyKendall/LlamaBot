"""
Rails Ticket Mode Agent using LangChain 1.1+ create_agent with ToolRuntime.

This agent converts user observations into implementation-ready engineering tickets:
- Stage 1: Story Collection - Enforces structured observation template
- Stage 2: Technical Research - Generates TECHNICAL_RESEARCH.md with codebase analysis
- Stage 3: Ticket Creation - Creates formatted ticket from research notes

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
    write_todos, ls, read_file, write_file, edit_file, search_file, bash_command,
    rails_api_sh,
)
from app.agents.leonardo.rails_ticket_mode_agent.prompts import TICKET_MODE_AGENT_PROMPT
from app.agents.leonardo.rails_ticket_mode_agent.middleware import (
    inject_view_context,
    inject_ticket_mode_context,
    check_failure_limit,
    DynamicModelMiddleware,
)
from app.agents.leonardo.rails_ticket_mode_agent.sub_agents import delegate_task

import logging
logger = logging.getLogger(__name__)


def get_cached_system_prompt():
    """Build system message with current date and prompt caching enabled.

    Date is appended at the end so the main prompt can be cached,
    and only the date portion changes daily.
    """
    current_date = date.today().strftime("%Y-%m-%d")
    prompt_with_date = f"{TICKET_MODE_AGENT_PROMPT}\n\n---\n**Today's Date:** {current_date}"

    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": prompt_with_date,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    )


WRITE_FINAL_TICKET_DESCRIPTION = """Create a ticket in the Rails database.

Parameters:
- title: Ticket title in format "YYYY-MM-DD - TYPE: Short Title" (e.g., "2025-01-25 - BUG: Line Item Rate Shows 0")
- description: User-facing content - Original User Story (THE CONTRACT with URL, Current/Desired Behavior, Verification Criteria, Business Rules), Demo Path, Scope, Metadata, User-Facing Summary
- ticket_type: One of: feature_new_model, feature_extend_existing_model, bug_debug, ux_copy, ux_layout, builder_integration
- research_notes: ALL technical/engineering details - Root Cause, Five Whys, DB Schema, Models, Controllers, UI Components, Code Health Observations
- notes: Implementation guidance - Implementation Notes, Test Plan, Constraints, Unresolved Questions, Split Check
- status: Ticket status (default: 'backlog')

Returns confirmation with the created ticket ID."""


@tool(description=WRITE_FINAL_TICKET_DESCRIPTION)
def write_final_ticket(
    title: str,
    description: str,
    ticket_type: str,
    runtime: ToolRuntime,
    research_notes: str = "",
    notes: str = "",
    status: str = "backlog",
) -> Command:
    """Create a ticket directly in the Rails database."""
    tool_call_id = runtime.tool_call_id

    # Base64 encode all text fields to avoid shell/Ruby escaping issues
    def b64(s: str) -> str:
        return base64.b64encode(s.encode('utf-8')).decode('ascii')

    title_b64 = b64(title)
    description_b64 = b64(description)
    research_notes_b64 = b64(research_notes)
    notes_b64 = b64(notes)

    # Ruby code that decodes base64 and creates the ticket
    ruby_script = f'''
require "base64"
ticket = LlamaBotRails::Ticket.create!(
  title: Base64.decode64("{title_b64}").force_encoding("UTF-8"),
  description: Base64.decode64("{description_b64}").force_encoding("UTF-8"),
  ticket_type: "{ticket_type}",
  research_notes: Base64.decode64("{research_notes_b64}").force_encoding("UTF-8"),
  notes: Base64.decode64("{notes_b64}").force_encoding("UTF-8"),
  status: "{status}"
)
puts "TICKET_CREATED:" + ticket.id.to_s
'''

    # Execute via rails_api_sh
    command = f"bin/rails runner '{ruby_script}'"
    result = rails_api_sh(command)

    # Parse result
    if "TICKET_CREATED:" in result:
        ticket_id = result.split("TICKET_CREATED:")[1].strip().split()[0]
        success_msg = f"Ticket created successfully with ID: {ticket_id}"
        tool_output = {"status": "success", "ticket_id": ticket_id}
        return Command(
            update={
                "messages": [ToolMessage(success_msg, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )
    else:
        error_msg = f"Failed to create ticket: {result}"
        tool_output = {"status": "error", "message": result}
        return Command(
            update={
                "messages": [ToolMessage(error_msg, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )


# Tool list - tools available to the Ticket Mode agent (NO internet_search)
default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file, search_file,
    bash_command,
    delegate_task,  # Sub-agent delegation for focused research tasks
    write_final_ticket,  # Creates ticket directly in Rails database
]


def build_workflow(checkpointer=None):
    """Build the Ticket Mode agent workflow with create_agent.

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
            trigger=("tokens", 80000),
            keep=("messages", 8),
            trim_tokens_to_summarize=None,  # KEY FIX: Disable trimming, let Gemini see everything
        ),
        # 2. Dynamic model selection based on state.llm_model from frontend
        DynamicModelMiddleware(),
        # 3. View path context injection - prepends page context to user messages
        inject_view_context,
        # 4. Ticket mode context - reminds agent of write restrictions
        inject_ticket_mode_context,
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
