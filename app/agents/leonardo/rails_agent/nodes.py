"""
Rails Agent using LangChain 1.1+ create_agent with ToolRuntime.

This agent helps non-technical users build Ruby on Rails applications with:
- Dynamic LLM model selection (Claude Haiku, Sonnet, GPT-5 Codex)
- Automatic context summarization for long sessions
- View path context injection (via middleware)
- Failure circuit breaker after 3 failed tool calls
- ToolRuntime for state access in tools
- Anthropic prompt caching for reduced latency and costs (via SystemMessage with cache_control)

Note: We use langchain.agents.create_agent with ToolRuntime pattern instead of
langgraph's InjectedState because create_agent provides middleware support.
"""

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from app.agents.leonardo.agent_factory import build_leonardo_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
from app.agents.leonardo.summarization import make_summarization_middleware
from langchain_core.messages import SystemMessage

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, write_file, edit_file,
    # search_file,
    glob_files, grep_files,
    bash_command, tail_rails_logs, hard_restart_rails, fix_permissions,
    git_status, git_commit, git_command, github_cli_command, internet_search,
    save_memory, list_memories, delete_memory,
    read_leonardo_md, edit_leonardo_md, write_leonardo_md,
    browser_inspect, browser_inspect_enabled,
)
from app.agents.leonardo.rails_agent.prompts import RAILS_AGENT_PROMPT
from app.agents.leonardo.project_context import build_system_prompt_with_project_context
from app.agents.leonardo.rails_agent.middleware import (
    inject_view_context,
    check_failure_limit,
    DynamicModelMiddleware,
    deepseek_reasoning_fix,
    strip_unsupported_multimodal,
    clear_old_tool_images,
    repair_orphaned_tool_calls,
)
from app.agents.utils.token_counter import (
    gemini_multimodal_token_counter_strip_images,
    SUMMARIZATION_TOKEN_THRESHOLD,
)
from app.agents.leonardo.rails_agent.sub_agents import delegate_task, delegate_research

import logging
logger = logging.getLogger(__name__)

def get_cached_system_prompt():
    """Build system message with project context and prompt caching.

    Loads LEONARDO.md if it exists and appends it to the base prompt.
    Uses Anthropic's ephemeral cache control for cost reduction (~90% input token savings).
    """
    full_prompt = build_system_prompt_with_project_context(RAILS_AGENT_PROMPT, agent_mode="rails_agent")
    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": full_prompt,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    )

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

# Tool list - all tools available to the Rails agent
default_tools = [
    write_todos,
    ls, read_file, write_file, edit_file,
    # search_file,
    glob_files, grep_files,
    bash_command,
    tail_rails_logs,
    hard_restart_rails,
    fix_permissions,
    # git_status, git_commit,
    # git_command,
    # github_cli_command,
    internet_search,
    delegate_task,      # Full-capability sub-agent for implementation work
    delegate_research,  # Read-only sub-agent for codebase investigation
    save_memory, list_memories, delete_memory,  # Long-term memory
    read_leonardo_md, edit_leonardo_md, write_leonardo_md,  # Project context file
    # browser_inspect is appended conditionally by agent_tools() — gated by the
    # `enable_browser_inspect` site setting (disabled by default).
]


def agent_tools():
    """The Rails agent's toolset, with browser_inspect gated by a site setting.

    browser_inspect (headless Chromium) is opt-in: only included when the
    `enable_browser_inspect` site setting is on. Disabled by default. Read at
    workflow build time, so flipping the setting takes effect on the next restart.
    """
    tools = list(default_tools)
    if browser_inspect_enabled():
        tools.append(browser_inspect)
        logger.info("browser_inspect tool enabled via site setting")
    return tools

def build_workflow(checkpointer=None, ask_before_edits=False):
    """Build the Rails agent workflow with create_agent.

    Args:
        checkpointer: Optional checkpointer for state persistence (e.g., PostgresSaver)
        ask_before_edits: If True, adds HumanInTheLoopMiddleware for destructive tools

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
        # 1. Repair orphaned tool calls — inject placeholder ToolMessages for any
        #    AIMessage tool_calls that have no response (e.g. from a Tavily crash).
        #    Must be first so all subsequent middleware and the model see valid history.
        repair_orphaned_tool_calls,
        # 2. Clear old browser_inspect screenshots from state BEFORE token counting.
        #    Each screenshot is ~25-50k tokens; without this they accumulate and keep the
        #    context above the summarization threshold on every turn → infinite loop.
        #    Must be before SummarizationMiddleware's before_model token counting.
        clear_old_tool_images,
        # 3. Summarization for long conversations. The shared factory wires the
        #    provider fallback model (DeepSeek->Gemini->OpenAI->Anthropic), a
        #    token-budgeted keep policy, screenshot-stripping token counting, and
        #    preservation of the first user messages + the live todo list. The
        #    REMOVE_ALL it emits only clears history because of the DeltaChannel
        #    reducer in app/agents/utils/delta_state.py (SupportIncident #106).
        make_summarization_middleware(summary_prompt=SUMMARIZATION_PROMPT),
        # 4. Dynamic model selection based on state.llm_model from frontend
        DynamicModelMiddleware(),
        # 5. Strip image/video/PDF blocks from history when the active model can't
        #    consume them (e.g. switching a vision thread onto text-only DeepSeek).
        strip_unsupported_multimodal,
        # 6. DeepSeek reasoning fix - injects reasoning_content for multi-turn tool calls
        deepseek_reasoning_fix,
        # 7. View path context injection - prepends page context to user messages
        inject_view_context,
        # 8. Circuit breaker - stop tool calls after 3 failures
        check_failure_limit,
    ]

    # Optional: Human-in-the-loop approval for destructive tools
    if ask_before_edits:
        DESTRUCTIVE_TOOLS = ['edit_file', 'write_file', 'bash_command']
        middleware.append(HumanInTheLoopMiddleware(
            interrupt_on={t: {"allowed_decisions": ["approve", "reject"]} for t in DESTRUCTIVE_TOOLS}
        ))

    # Create and return the agent
    return build_leonardo_agent(
        model=default_model,
        tools=agent_tools(),
        system_prompt=get_cached_system_prompt(),
        state_schema=RailsAgentState,
        middleware=middleware,
        checkpointer=checkpointer,
    )
