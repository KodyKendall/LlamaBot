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
from app.agents.leonardo.llm_factory import make_summarization_model
from app.agents.leonardo.agent_factory import build_leonardo_agent
from langchain.agents.middleware import SummarizationMiddleware
from app.agents.leonardo.summarization import make_summarization_middleware
from langchain_core.messages import SystemMessage, ToolMessage
from langchain.tools import tool, ToolRuntime
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict
from datetime import date

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, write_file, edit_file, search_file, bash_command,
    tail_rails_logs, hard_restart_rails,
    glob_files, grep_files, internet_search,
    read_leonardo_md, write_leonardo_md, edit_leonardo_md,
    save_memory, list_memories, delete_memory,
    build_use_skill_tool, list_skills, read_skill, write_skill, edit_skill, delete_skill,
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
from app.agents.utils.token_counter import SUMMARIZATION_TOKEN_THRESHOLD
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
   - The internal test plan (hidden `rails/requirements/.test_plan_*.md` file) and which tests are written / passing / failing
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
6. Internal Test Plan: The hidden test plan file (`rails/requirements/.test_plan_*.md`), the tests it specifies, and which are written/passing/failing
7. Implementation Progress: TODOs completed vs pending, files modified
8. Files and Code Sections: Files examined, modified, or created with summaries
9. Errors and Fixes: Problems encountered and resolutions
10. All User Messages: List ALL non-tool-result user messages
11. Pending Tasks: What still needs to be done
12. Current Work: What was being worked on immediately before this summary
13. Next Step: The immediate next action aligned with the current phase

# Conversation to summarize:
{messages}
"""


def get_cached_system_prompt():
    """Build system message with project context, personality files, date, and prompt caching."""
    current_date = date.today().strftime("%Y-%m-%d")
    date_suffix = f"\n\n---\n**Today's Date:** {current_date}"
    full_prompt = build_beginner_system_prompt(
        PLAN_MODE_AGENT_PROMPT,
        suffix=date_suffix,
        agent_mode="rails_plan_mode_agent",
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
- ui_related: (Optional, default false) Set to true when the question involves a VISUAL or
  UI/UX decision (layout, colors, components, button/card styling, section arrangement, etc.).
  When true, the user is shown one extra subtle "See visual options" choice alongside your
  options. If they pick it, the tool result will explicitly ask you to follow up by calling
  ask_user_uiux_question with 2-4 concrete live HTML previews for this decision. Leave it
  false for non-visual questions.

This tool will freeze execution and wait for the user to respond. The user's answer is returned as the tool result."""


@tool(description=ASK_USER_QUESTION_DESCRIPTION)
def ask_user_question(
    question: str,
    runtime: ToolRuntime,
    options: list[str] = None,
    context: str = "",
    ui_related: bool = False,
) -> Command:
    """Ask the user a question, freeze execution, and resume with their answer."""
    tool_call_id = runtime.tool_call_id

    # interrupt() freezes the agent here. The value is sent to the frontend
    # as a question_request. When the user answers, interrupt() returns the answer.
    # ui_related toggles an extra "See visual options" choice on the frontend card; if
    # the user picks it the resumed answer asks us to call ask_user_uiux_question next.
    user_answer = interrupt({
        "type": "user_question",
        "question": question,
        "options": options or [],
        "context": context,
        "ui_related": ui_related,
    })

    return Command(
        update={
            "messages": [ToolMessage(
                content=f"User answered: {user_answer}",
                tool_call_id=tool_call_id
            )]
        }
    )


class UIUXOption(TypedDict):
    """One visual option the user can pick from in ask_user_uiux_question.

    Attributes:
        id: Stable identifier returned (with the label) when this option is chosen.
        label: Short human-readable label shown next to the radio button.
        html: A self-contained HTML snippet rendered as a live preview inside a
            sandboxed iframe. Style it with INLINE styles for anything visual — colors,
            fonts, sizes, spacing (e.g. '<button style="background:#BE0000;color:#fff;
            padding:8px 16px;border-radius:6px;">Save</button>'). See
            ASK_USER_UIUX_QUESTION_DESCRIPTION for the full styling rules of thumb. 
            Do NOT include emojis unless explicitly asked, instead use icons.
            Try to use the existing branding, and look/feel of the current page they're on, unless they explicitly
            are asking for something different.
    """

    id: str
    label: str
    html: str


ASK_USER_UIUX_QUESTION_DESCRIPTION = """Ask the user to choose between VISUAL UI/UX options by showing them live previews.

Use this instead of ask_user_question whenever the choice is about how something LOOKS —
e.g. picking between layouts, components, button styles, card designs, color treatments, or
section arrangements. Each option is rendered as a live, isolated preview the user picks by sight.

IMPORTANT: Ask ONE question per tool call. Wait for the answer before asking the next.

Parameters:
- question: The question to ask, in plain non-technical language (e.g. "Which hero layout do you prefer?").
- options: A list of 2-4 visual options. Each option is an object with:
    - id: a short stable identifier (e.g. "centered", "split", "minimal")
    - label: a short human label for the option (e.g. "Centered hero")
    - html: a SELF-CONTAINED HTML snippet for the preview (see styling rules below).
  Do NOT add your own "none"/"other"/"something else" option — the UI automatically appends
  a "None of these" choice the user can pick (and explain). Only provide the real designs.
- context: (Optional) Brief context about why you're asking (shown as a subtitle).

STYLING RULES OF THUMB (the preview iframe is locked-down and does NOT behave like the
real app — these are hard-won, follow them or the preview renders blank/unstyled):
- PREFER PLAIN INLINE STYLES for everything visual. Colors, fonts, sizes, spacing, borders
  must go in a `style="..."` attribute (e.g. style="background:#BE0000;color:#fff;
  font-family:sans-serif;padding:8px 16px"). Inline styles are the only thing guaranteed
  to render. When in doubt, inline it.
- DO NOT rely on Tailwind utility classes — especially arbitrary-value classes like
  `bg-[#BE0000]`, `text-[20px]`, `w-[300px]`, `p-[12px]`. The preview only has a
  precompiled Tailwind v2 stylesheet; v3-style bracket/arbitrary-value classes do NOT
  exist in it and render as nothing. Use inline styles instead.
- NO DaisyUI or other component-library classes (`btn`, `card`, `hero`, `badge`,
  `bg-base-*`, etc.) — they won't render.
- NO <script> tags, NO external images/fonts/stylesheets, no app-specific CSS. A small
  inline <style> block (plain CSS) is fine if inline attributes get unwieldy.
- FOR ICONS, write INLINE <svg> directly into the snippet — do NOT load an icon font
  (Font Awesome, Material Icons, etc.) from a CDN; external fonts/stylesheets don't load
  in this iframe and the icons render blank. Inline SVG is plain text the browser draws
  with no loading. (In the REAL app you can still use the `fas fa-*` classes already on
  the page — inline SVG is only needed for the preview.)
- Keep snippets small, self-contained, and focused on the one visual decision being made.
- Do NOT include emojis unless explicitly asked, instead use icons.
- Try to use the existing branding, and look/feel of the current page they're on, unless they explicitly are asking for something different.

(If you discover another preview-rendering quirk, add it to this list — these accumulate.)

This tool freezes execution and waits. The user's chosen option (id and label) is returned as the tool result.
"""


@tool(description=ASK_USER_UIUX_QUESTION_DESCRIPTION)
def ask_user_uiux_question(
    question: str,
    options: list[UIUXOption],
    runtime: ToolRuntime,
    context: str = "",
) -> Command:
    """Ask the user to pick a visual option, freeze execution, and resume with their choice."""
    tool_call_id = runtime.tool_call_id

    # interrupt() freezes the agent here. The value is sent to the frontend as a
    # uiux_question_request. When the user picks an option, interrupt() returns their choice.
    user_answer = interrupt({
        "type": "uiux_question",
        "question": question,
        "options": options or [],
        "context": context,
    })

    return Command(
        update={
            "messages": [ToolMessage(
                content=f"User selected: {user_answer}",
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
    ask_user_uiux_question,
    # Standard tools (same as beginner + search_file)
    write_todos,
    ls, read_file, write_file, edit_file, search_file, bash_command,
    tail_rails_logs, hard_restart_rails,
    glob_files, grep_files, internet_search,
    read_leonardo_md, write_leonardo_md, edit_leonardo_md,
    save_memory, list_memories, delete_memory,
    list_skills, read_skill, write_skill, edit_skill, delete_skill,  # Skill library
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
    middleware = [
        # 1. Summarization for long conversations (shared factory: provider
        #    fallback model, token-budgeted keep, first-user-messages + todo
        #    preservation; REMOVE_ALL honored by the DeltaChannel reducer).
        make_summarization_middleware(summary_prompt=SUMMARIZATION_PROMPT),
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
    return build_leonardo_agent(
        model=default_model,
        tools=[*default_tools, build_use_skill_tool()],
        system_prompt=get_cached_system_prompt(),
        state_schema=RailsAgentState,
        middleware=middleware,
        checkpointer=checkpointer,
    )
