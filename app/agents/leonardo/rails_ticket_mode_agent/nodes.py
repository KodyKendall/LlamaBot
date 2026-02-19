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
from app.agents.leonardo.project_context import build_system_prompt_with_project_context
from app.agents.leonardo.rails_ticket_mode_agent.middleware import (
    inject_view_context,
    inject_ticket_mode_context,
    check_failure_limit,
    DynamicModelMiddleware,
)
from app.agents.utils.token_counter import gemini_multimodal_token_counter, SUMMARIZATION_TOKEN_THRESHOLD
from app.agents.leonardo.rails_ticket_mode_agent.sub_agents import delegate_task

import logging
logger = logging.getLogger(__name__)

# Detailed summarization prompt for context extraction
# Creates structured summaries that preserve technical details, code patterns, and user intent
SUMMARIZATION_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
  - Errors that you ran into and how you fixed them
  - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
   If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
    - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
    - [Detailed non tool use user message]
    - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.

There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating the above summary.

# Summary Instructions
When summarizing the conversation, focus on Ruby on Rails code changes including models, controllers, views, migrations, and routes. Include RSpec test output and remember the mistakes you made and how you fixed them.

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
        TICKET_MODE_AGENT_PROMPT,
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
            trigger=("tokens", SUMMARIZATION_TOKEN_THRESHOLD),
            keep=("messages", 20),  # Match Claude Code's default
            token_counter=gemini_multimodal_token_counter,
            trim_tokens_to_summarize=None,  # KEY FIX: Disable trimming, let Gemini see everything
            summary_prompt=SUMMARIZATION_PROMPT,
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
