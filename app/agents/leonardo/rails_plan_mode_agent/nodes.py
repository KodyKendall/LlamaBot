"""
Rails Plan Mode Agent using LangChain 1.1+ create_agent with ToolRuntime.

This agent guides non-technical users through a 6-phase workflow:
- Phase 1: Clarify - Ask plain-language questions about what the user wants
- Phase 2: Research - Explore the codebase to understand current state
- Phase 3: Refine - Ask follow-up questions based on research findings
- Phase 4: Present Plan - Write a non-technical plan and get approval
- Phase 5: Implement - Build everything following the plan rigorously
- Phase 6: Verify & Done - Run tests, summarize results

Features:
- Dynamic LLM model selection (defaults to DeepSeek V4 Flash)
- Automatic context summarization for long sessions
- View path context injection (via middleware)
- Failure circuit breaker after 3 failed tool calls
- ask_user_question tool for structured user interaction
- Anthropic prompt caching for reduced latency and costs
"""

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import SystemMessage, ToolMessage
from langchain.tools import tool, ToolRuntime
from langgraph.types import Command, interrupt
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
from app.agents.leonardo.rails_plan_mode_agent.prompts import PLAN_MODE_AGENT_PROMPT
from app.agents.leonardo.project_context import build_beginner_system_prompt
from app.agents.leonardo.rails_plan_mode_agent.middleware import (
    inject_view_context,
    inject_plan_mode_context,
    check_failure_limit,
    DynamicModelMiddleware,
)
from app.agents.utils.token_counter import gemini_multimodal_token_counter, SUMMARIZATION_TOKEN_THRESHOLD
from app.agents.leonardo.rails_agent.sub_agents import delegate_task, delegate_research

import logging
logger = logging.getLogger(__name__)

# Summarization prompt for plan mode conversations
SUMMARIZATION_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing the planning workflow phase, user requirements, and implementation progress.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Which phase of the plan mode workflow we are in (Clarify, Research, Refine, Present Plan, Implement, Verify)
   - Questions asked and answers received
   - Research findings
   - The plan that was created (if any)
   - Implementation progress and TODO status
   - Specific details like file names, code snippets, and edits made
   - Errors encountered and how they were fixed
   - User feedback and corrections

2. Double-check for technical accuracy and completeness.

Your summary should include:

1. Primary Request and Intent: What the user wants built
2. Current Phase: Which of the 6 phases we are in
3. Questions & Answers: All clarifying questions asked and user responses
4. Research Findings: What was discovered about the codebase
5. The Plan: The full plan if one was created (file path + content summary)
6. Implementation Progress: TODOs completed vs pending, files modified
7. Files and Code Sections: Files examined, modified, or created with summaries
8. Errors and Fixes: Problems encountered and resolutions
9. All User Messages: List ALL non-tool-result user messages
10. Pending Tasks: What still needs to be done
11. Current Work: What was being worked on immediately before this summary
12. Next Step: The immediate next action aligned with the current phase

# Conversation to summarize:
{messages}
"""


def get_cached_system_prompt():
    """Build system message with project context, personality files, date, and prompt caching."""
    current_date = date.today().strftime("%Y-%m-%d")
    date_suffix = f"\n\n---\n**Today's Date:** {current_date}"
    full_prompt = build_beginner_system_prompt(
        PLAN_MODE_AGENT_PROMPT,
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
# Plan Mode Tools
# =============================================================================

ASK_USER_QUESTION_DESCRIPTION = """Ask the user ONE question at a time. Use this to:
- Clarify what they want (Phase 1: Clarify)
- Ask follow-up questions after research (Phase 3: Refine)
- Get approval for the plan (Phase 4: Present Plan)

IMPORTANT: Only ask ONE question per tool call. If you have multiple questions, call this tool once, wait for the answer, then ask the next question. This keeps it simple and non-overwhelming for the user.

Parameters:
- question: The question to ask, in plain non-technical language
- options: (Optional) A list of suggested answers the user can pick from. The user can also type their own answer. Use this to make it easy for non-technical users to respond.
- context: (Optional) Brief context about why you're asking (shown as a subtitle)

This tool will freeze execution and wait for the user to respond. The user's answer is returned as the tool result."""


@tool(description=ASK_USER_QUESTION_DESCRIPTION)
def ask_user_question(
    question: str,
    runtime: ToolRuntime,
    options: list[str] = None,
    context: str = "",
) -> Command:
    """Ask the user a question, freeze execution, and resume with their answer."""
    tool_call_id = runtime.tool_call_id

    # interrupt() freezes the agent here. The value is sent to the frontend
    # as a question_request. When the user answers, interrupt() returns the answer.
    user_answer = interrupt({
        "type": "user_question",
        "question": question,
        "options": options or [],
        "context": context,
    })

    return Command(
        update={
            "messages": [ToolMessage(
                content=f"User answered: {user_answer}",
                tool_call_id=tool_call_id
            )]
        }
    )


# =============================================================================
# Tool list and workflow
# =============================================================================

default_tools = [
    # Plan mode specific
    ask_user_question,
    # Standard tools (same as beginner + search_file)
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
    """Build the Plan Mode agent workflow with create_agent.

    Args:
        checkpointer: Optional checkpointer for state persistence (e.g., PostgresSaver)

    Returns:
        A compiled LangGraph agent
    """
    # Default model (will be overridden by DynamicModelMiddleware based on state.llm_model)
    default_model = ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)

    # Configure middleware stack (order matters - executed top to bottom)
    summarization_model = ChatGoogleGenerativeAI(
        model="gemini-3-flash-preview",
        vertexai=False,
        temperature=1.0,
    )
    middleware = [
        # 1. Summarization for long conversations
        SummarizationMiddleware(
            model=summarization_model,
            trigger=("tokens", SUMMARIZATION_TOKEN_THRESHOLD),
            keep=("messages", 20),
            token_counter=gemini_multimodal_token_counter,
            trim_tokens_to_summarize=None,
            summary_prompt=SUMMARIZATION_PROMPT,
        ),
        # 2. Dynamic model selection based on state.llm_model from frontend
        DynamicModelMiddleware(),
        # 3. View path context injection
        inject_view_context,
        # 4. Plan mode context - reminds agent of phase workflow
        inject_plan_mode_context,
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
