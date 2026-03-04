"""
Sub-Agent Spawning for Rails User Feedback Agent (User Mode v2).

This module provides the `delegate_task` tool that allows the main User Mode agent
to spawn sub-agents for focused RESEARCH work with isolated context.

IMPORTANT: Sub-agents are RESEARCH-ONLY. They do NOT create feedback entries.
The main agent is responsible for calling write_feedback after receiving research.

Use cases:
- Technical research on the Rails codebase
- Investigating specific files or patterns in isolation
- Database schema analysis
- Model/controller/view exploration
- Answering complex questions about how the app works
"""

from langchain.agents import create_agent
from langchain.tools import tool, ToolRuntime
from langchain_core.messages import ToolMessage, SystemMessage
from langgraph.types import Command
from datetime import date
import logging

# Import tools for research (NO write_file - sub-agent should only read)
from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, search_file, bash_command, glob_files, grep_files
)
# Import the model factory from middleware to use the same model as the main agent
from app.agents.leonardo.rails_agent.middleware import DynamicModelMiddleware

logger = logging.getLogger(__name__)


# =============================================================================
# Research-Only Prompt for Sub-Agent
# =============================================================================

SUB_AGENT_RESEARCH_PROMPT = """You are a technical research agent for Rails codebases.

## YOUR ROLE
You are delegated focused research tasks by the main User Mode agent.
Your job is to investigate the codebase and RETURN your findings to help answer user questions.

## CRITICAL RULES
1. **DO NOT create feedback entries** - The main agent handles that
2. **DO NOT write files** - You are read-only
3. **RETURN structured findings** - The main agent needs your research
4. **Be thorough but concise** - Find the relevant information efficiently

## RESEARCH METHODOLOGY

### For "How does X work?" questions:
1. Find the relevant files (schema.rb, models, controllers, views)
2. Trace the data flow from user input to database to display
3. Identify key callbacks, associations, and business logic
4. Summarize in plain language

### For investigating issues/bugs:
1. Locate the relevant code paths
2. Identify what the code is doing vs what it should do
3. Note any potential causes
4. Document file paths and line numbers

### For understanding features:
1. Start with schema.rb to understand the data model
2. Read the relevant model(s) for business logic
3. Check controllers for request handling
4. Review views for UI behavior

## OUTPUT FORMAT

Return your findings in a clear, organized format:

### Summary
[1-2 sentence answer to the research question]

### Key Files
| File | Purpose |
|------|---------|
| path/to/file.rb | Brief description |

### Technical Details
[Detailed explanation with code references]

### Relevant Code Snippets
```ruby
# file_path:line_number
code here
```

### Additional Notes
[Any gotchas, edge cases, or related information]

---
**Remember: RETURN your findings. DO NOT create feedback or write files.**
"""


def get_research_system_prompt():
    """Build system message for research-only sub-agent."""
    current_date = date.today().strftime("%Y-%m-%d")
    prompt_with_date = f"{SUB_AGENT_RESEARCH_PROMPT}\n\n---\n**Today's Date:** {current_date}"
    return SystemMessage(content=prompt_with_date)


# =============================================================================
# Sub-Agent Factory
# =============================================================================

def create_sub_agent(llm_model: str = None):
    """Create a RESEARCH-ONLY sub-agent instance.

    The sub-agent uses:
    - Research-only system prompt (NOT the full user mode prompt)
    - Read-only tools (no write_file, no edit_file, no write_feedback)
    - Same LLM model as the main agent (passed from state.llm_model)

    IMPORTANT: Sub-agents do NOT create feedback. They only research and return findings.
    The main agent is responsible for calling write_feedback.

    Args:
        llm_model: The model name from state.llm_model (e.g., 'claude-4.5-haiku', 'gemini-3-flash').
                   If None, defaults to 'gemini-3-flash'.

    Returns:
        A compiled LangGraph research agent
    """
    # RESEARCH-ONLY tools - no write_file, no edit_file, no write_feedback
    # Sub-agent should only READ the codebase, not modify it
    sub_agent_tools = [
        write_todos,
        ls,
        read_file,
        search_file,
        glob_files,
        grep_files,
        bash_command,  # For rails runner queries (read-only DB queries)
        # NO write_file - sub-agent should not write files
        # NO edit_file - sub-agent should not edit files
        # NO write_feedback - main agent handles feedback creation
        # NO delegate_task - prevent infinite recursion
    ]

    # Use the same model as the main agent by reusing DynamicModelMiddleware's _get_llm
    model_middleware = DynamicModelMiddleware()
    model = model_middleware._get_llm(llm_model or 'gemini-3-flash')

    return create_agent(
        model=model,
        tools=sub_agent_tools,
        system_prompt=get_research_system_prompt(),  # Research-only prompt
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
    """Delegate a RESEARCH task to a sub-agent with isolated context.

    IMPORTANT: The sub-agent is RESEARCH-ONLY. It will NOT create feedback entries.
    After receiving the research findings, YOU (the main agent) should use the
    findings to answer the user's question or create feedback if appropriate.

    When to use:
    - Complex questions about how the app works
    - Investigating database schema, models, controllers, views
    - Understanding data flow or business logic
    - Researching the codebase to answer user questions

    Args:
        task_description: Clear, detailed description of what needs to be researched.
            Include: specific questions to answer, files to investigate,
            or patterns to look for.

    Returns:
        Structured research findings including:
        - Summary answer
        - Key files and their purposes
        - Technical details with code references
        - Relevant code snippets
        - Additional notes

    Example:
        delegate_task(
            task_description="Research how the Order model calculates totals. Look at rails/app/models/order.rb and any related callbacks or associations."
        )

        delegate_task(
            task_description="Find all the places where user permissions are checked. Search for authorization logic in controllers."
        )

    After receiving findings, use them to answer the user's question or draft feedback.
    """
    tool_call_id = runtime.tool_call_id
    # Get the llm_model from the main agent's state so sub-agent uses the same model
    llm_model = runtime.state.get('llm_model')
    logger.info(f"User Mode delegating task to sub-agent (model: {llm_model}): {task_description[:100]}...")

    try:
        sub_agent = create_sub_agent(llm_model=llm_model)

        # Invoke with the task - sub-agent starts with fresh context
        result = sub_agent.invoke({
            "messages": [{"role": "user", "content": task_description}]
        })

        # Extract the final response from the sub-agent
        final_message = result["messages"][-1].content if result["messages"] else "No response from sub-agent"

        logger.info("User Mode sub-agent completed task successfully")

        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"[DELEGATED RESEARCH COMPLETED]\n\n{final_message}",
                    tool_call_id=tool_call_id
                )
            ]
        })

    except Exception as e:
        logger.error(f"User Mode sub-agent failed: {str(e)}")
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"[DELEGATED RESEARCH FAILED]\n\nError: {str(e)}",
                    tool_call_id=tool_call_id
                )
            ],
            "failed_tool_calls_count": 1  # Increment failure counter
        })
