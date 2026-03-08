"""
Sub-Agent Spawning for Rails Agent.

This module provides TWO delegation tools:

1. `delegate_task` - Full-capability sub-agent for implementation work
   - Has all tools (write, edit, bash, git)
   - Same prompt as main agent
   - Use for: implementing features, making changes in isolation

2. `delegate_research` - Research-only sub-agent for investigation
   - Read-only tools (ls, read_file, grep, glob, bash for queries)
   - Special research-focused prompt
   - Use for: exploring codebase, understanding patterns, root cause analysis
   - CANNOT write files, edit code, or make commits

The purpose is context isolation: keeping the main agent's tool call
history clean while delegating specific tasks to a fresh agent instance.
"""

from langchain.agents import create_agent
from langchain.tools import tool, ToolRuntime
from langchain_core.messages import ToolMessage, SystemMessage
from langgraph.types import Command
from datetime import date
import logging

# Import the same tools, state, and prompt used by the main agent
from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.prompts import RAILS_AGENT_PROMPT
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, write_file, edit_file,
    search_file, bash_command, git_status, git_commit,
    grep_files, glob_files,
    git_command, github_cli_command, internet_search
)
# Import the model factory from middleware to use the same model as the main agent
from app.agents.leonardo.rails_agent.middleware import DynamicModelMiddleware

logger = logging.getLogger(__name__)


# =============================================================================
# Research-Only Prompt for delegate_research
# =============================================================================

RESEARCH_ONLY_PROMPT = """You are a specialized RESEARCH agent for Rails codebases.

## YOUR ROLE
You are delegated focused research tasks by the main Rails agent.
Your job is to investigate the codebase and RETURN your findings.
The main agent will use your findings to decide what changes to make.

## CRITICAL CONSTRAINTS
- **DO NOT write files** - You are READ-ONLY
- **DO NOT edit files** - You cannot modify code
- **DO NOT make commits** - You have no git write access
- **DO NOT execute destructive commands** - Only read-only bash queries
- **RETURN findings** - The main agent uses your research to decide next steps

## RESEARCH TOOLS AVAILABLE
- `read_file` - Read file contents (USE THIS, not `cat`)
- `glob_files` - Find files by pattern (USE THIS, not `find`)
- `grep_files` - Search file contents by regex (USE THIS, not `grep`)
- `ls` - List directory contents
- `bash_command` - Run read-only Rails commands (e.g., `rails runner "puts User.count"`)
- `write_todos` - Track your research progress

## ⛔ FORBIDDEN BASH COMMANDS
NEVER use these bash commands - use the dedicated tools instead:

| ❌ NEVER USE | ✅ USE INSTEAD |
|--------------|----------------|
| `cat file.rb` | `read_file` tool |
| `head -50 file.rb` | `read_file` with limit param |
| `tail -20 file.rb` | `read_file` with offset param |
| `grep "pattern" file` | `grep_files` tool |
| `find . -name "*.rb"` | `glob_files` tool |

**Exception:** Piping output through `head`/`tail` IS allowed to limit command output:
```bash
bundle exec rails runner "puts User.all" | tail -20   # ✅ OK
```

`bash_command` is ONLY for: Rails runner queries, rake tasks, and system queries.

## RESEARCH METHODOLOGY

### For Bug Investigation:
1. **Root Cause Layer Classification** - Is it DB, Model, Controller, or View?
2. **Five Whys Analysis** - Trace the symptom back to root cause
3. **Evidence Gathering** - File paths, line numbers, code snippets

### For Feature Research:
1. **Existing Patterns** - How is similar functionality implemented?
2. **Data Model** - What tables/columns are involved?
3. **Code Flow** - Controller → Model → View flow

### For General Exploration:
1. **File Structure** - Where are the relevant files?
2. **Associations** - How do models relate?
3. **Routes** - What endpoints exist?

## OUTPUT FORMAT

Return findings in clear, structured format:

### Summary
[1-2 sentence overview of what you found]

### Key Files
| File | Purpose | Key Lines |
|------|---------|-----------|
| path/to/file.rb | What it does | Lines X-Y |

### Technical Details
[Detailed findings with code references]

### Code Snippets
```ruby
# Relevant code with file:line references
```

### Gotchas / Edge Cases
[Any issues or considerations the main agent should know]

---
**Remember: You are READ-ONLY. Return your findings so the main agent can act on them.**
"""


def get_research_system_prompt():
    """Build system message for research-only sub-agent with prompt caching."""
    current_date = date.today().strftime("%Y-%m-%d")
    prompt_with_date = f"{RESEARCH_ONLY_PROMPT}\n\n---\n**Today's Date:** {current_date}"
    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": prompt_with_date,
                "cache_control": {"type": "ephemeral"}  # Enable Anthropic prompt caching
            }
        ]
    )

# System message with Anthropic prompt caching enabled (same as main agent)
# This caches the system prompt for 5 minutes, reducing input token costs by ~90%
CACHED_SYSTEM_PROMPT = SystemMessage(
    content=[
        {
            "type": "text",
            "text": RAILS_AGENT_PROMPT,
            "cache_control": {"type": "ephemeral"}
        }
    ]
)


# =============================================================================
# Sub-Agent Factory
# =============================================================================

def create_sub_agent(llm_model: str = None):
    """Create a sub-agent instance with the same config as the main Rails agent.

    The sub-agent uses:
    - Same system prompt (RAILS_AGENT_PROMPT) with prompt caching
    - Same tools (all tools from tools.py)
    - Same state schema (RailsAgentState)
    - Same LLM model as the main agent (passed from state.llm_model)

    The only difference is it runs in isolated context without the main
    conversation's message history.

    Args:
        llm_model: The model name from state.llm_model (e.g., 'claude-4.5-haiku', 'gemini-3-flash').
                   If None, defaults to 'gemini-3-flash'.

    Returns:
        A compiled LangGraph agent identical to the main Rails agent
    """
    # Same tools as the main agent (minus delegate_task to prevent recursion)
    sub_agent_tools = [
        write_todos,
        ls, read_file, write_file, edit_file,
        #search_file,
        grep_files, glob_files,
        bash_command,
        git_status, git_commit, git_command, github_cli_command,
        internet_search,
        # Note: delegate_task is NOT included to prevent infinite recursion
    ]

    # Use the same model as the main agent by reusing DynamicModelMiddleware's _get_llm
    model_middleware = DynamicModelMiddleware()
    model = model_middleware._get_llm(llm_model or 'gemini-3-flash')

    return create_agent(
        model=model,
        tools=sub_agent_tools,
        system_prompt=CACHED_SYSTEM_PROMPT,  # With prompt caching
        state_schema=RailsAgentState,
    )


# =============================================================================
# Delegate Task Tool
# =============================================================================

@tool("delegate_task")
def delegate_task(
    task_description: str,
    runtime: ToolRuntime,
) -> Command:
    """Delegate a task to a sub-agent with isolated context.

    Use this tool when you want to spin off a focused task to a fresh agent
    instance, keeping your main conversation clean. The sub-agent has the same
    capabilities as you (same tools, same prompt) but starts with fresh context.

    When to use:
    - Research tasks that would add noise to your main conversation
    - Implementing a specific feature where you want isolated focus
    - Any task where you want clean context without your current tool call history
    - When your context is getting cluttered and you want a fresh start on a subtask

    Args:
        task_description: Clear, detailed description of what needs to be done.
            Include all relevant context the sub-agent needs (file paths,
            requirements, acceptance criteria). Be specific about the expected
            outcome since the sub-agent doesn't have your conversation history.

    Returns:
        A Command with the sub-agent's completion message and summary of work done.

    Example:
        delegate_task(
            task_description="Research how to implement soft deletes in Rails. Look at the paranoia gem and acts_as_paranoid. Summarize the best approach for our Posts model in db/schema.rb."
        )

        delegate_task(
            task_description="Create a migration to add an 'archived' boolean column to the posts table (see db/schema.rb) with a default of false. Then update the Post model (app/models/post.rb) to add a scope for archived posts."
        )
    """
    tool_call_id = runtime.tool_call_id
    # Get the llm_model from the main agent's state so sub-agent uses the same model
    llm_model = runtime.state.get('llm_model')
    logger.info(f"Delegating task to sub-agent (model: {llm_model}): {task_description[:100]}...")

    try:
        sub_agent = create_sub_agent(llm_model=llm_model)

        # Invoke with the task - sub-agent starts with fresh context
        result = sub_agent.invoke({
            "messages": [{"role": "user", "content": task_description}]
        })

        # Extract the final response from the sub-agent
        final_message = result["messages"][-1].content if result["messages"] else "No response from sub-agent"

        logger.info("Sub-agent completed task successfully")

        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"[DELEGATED TASK COMPLETED]\n\n{final_message}",
                    tool_call_id=tool_call_id
                )
            ]
        })

    except Exception as e:
        logger.error(f"Sub-agent failed: {str(e)}")
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"[DELEGATED TASK FAILED]\n\nError: {str(e)}",
                    tool_call_id=tool_call_id
                )
            ],
            "failed_tool_calls_count": 1  # Increment failure counter
        })


# =============================================================================
# Research-Only Sub-Agent Factory
# =============================================================================

def create_research_sub_agent(llm_model: str = None):
    """Create a RESEARCH-ONLY sub-agent instance.

    The research sub-agent uses:
    - Research-only system prompt (NOT the full Rails agent prompt)
    - Read-only tools (no write_file, edit_file, git tools)
    - Same LLM model as the main agent (passed from state.llm_model)

    IMPORTANT: Research sub-agents CANNOT modify the codebase.
    They only investigate and return findings.

    Args:
        llm_model: The model name from state.llm_model (e.g., 'claude-4.5-haiku', 'gemini-3-flash').
                   If None, defaults to 'gemini-3-flash'.

    Returns:
        A compiled LangGraph research agent with read-only capabilities
    """
    # RESEARCH-ONLY tools - strictly read-only
    research_tools = [
        write_todos,  # Track research progress
        ls,           # List directories
        read_file,    # Read file contents
        glob_files,   # Find files by pattern
        grep_files,   # Search file contents
        bash_command, # For read-only Rails queries (e.g., rails runner)
        # NO write_file - cannot write files
        # NO edit_file - cannot edit files
        # NO git tools - cannot make commits
        # NO delegate_task/delegate_research - prevent recursion
    ]

    # Use the same model as the main agent
    model_middleware = DynamicModelMiddleware()
    model = model_middleware._get_llm(llm_model or 'gemini-3-flash')

    return create_agent(
        model=model,
        tools=research_tools,
        system_prompt=get_research_system_prompt(),  # Research-only prompt
        state_schema=RailsAgentState,
    )


# =============================================================================
# Delegate Research Tool (Read-Only)
# =============================================================================

@tool("delegate_research")
def delegate_research(
    task_description: str,
    runtime: ToolRuntime,
) -> Command:
    """Delegate a RESEARCH-ONLY task to a specialized sub-agent.

    Use this tool when you need to investigate the codebase without making changes.
    The research sub-agent has READ-ONLY access and will return structured findings.

    IMPORTANT: The research sub-agent CANNOT:
    - Write or edit files
    - Make git commits
    - Execute destructive commands

    It CAN:
    - Read files and directories
    - Search with grep/glob
    - Run read-only Rails queries (rails runner)
    - Return structured research findings

    When to use delegate_research:
    - Finding where code is defined across multiple files
    - Understanding how a feature works (tracing data flow)
    - Analyzing database schema, models, controllers, views
    - Root cause analysis for bugs
    - Exploring existing patterns before making changes
    - Any investigation that would pollute your main context

    When NOT to use (use delegate_task instead):
    - You need to implement a feature
    - You need to make code changes
    - You need full tool access

    When NOT to use (just read directly):
    - You know the exact file to read (just use read_file)
    - You need to read 1-2 specific files

    Args:
        task_description: Clear description of what to research, including:
            - What you're looking for
            - Why it matters (context for the research)
            - Any file hints or starting points

    Returns:
        Structured research findings with file locations and code references.

    Examples:
        delegate_research(
            task_description="Find where user_id assignments happen for Jobs. "
            "I need to understand why jobs are being assigned to the wrong user. "
            "Check the Job model, any import tasks, and seeds."
        )

        delegate_research(
            task_description="Research how Turbo Streams are used in the tenders views. "
            "I need to add a new broadcast and want to follow existing patterns. "
            "Look at app/views/tenders/ and the Tender model callbacks."
        )
    """
    tool_call_id = runtime.tool_call_id
    llm_model = runtime.state.get('llm_model')
    logger.info(f"Delegating RESEARCH to sub-agent (model: {llm_model}): {task_description[:100]}...")

    try:
        sub_agent = create_research_sub_agent(llm_model=llm_model)

        # Invoke with fresh context - sub-agent only sees the task description
        result = sub_agent.invoke({
            "messages": [{"role": "user", "content": task_description}]
        })

        # Extract the final response from the sub-agent
        final_message = result["messages"][-1].content if result["messages"] else "No response from research sub-agent"

        logger.info("Research sub-agent completed task successfully")

        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"[RESEARCH COMPLETED]\n\n{final_message}",
                    tool_call_id=tool_call_id
                )
            ]
        })

    except Exception as e:
        logger.error(f"Research sub-agent failed: {str(e)}")
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"[RESEARCH FAILED]\n\nError: {str(e)}",
                    tool_call_id=tool_call_id
                )
            ],
            "failed_tool_calls_count": 1  # Increment failure counter
        })
