"""
Sub-Agent Spawning for Rails Ticket Mode Agent.

This module provides the `delegate_task` tool that allows the main Ticket Mode agent
to spawn sub-agents for focused work with isolated context.

Sub-agents are NOT specialized - they use the same prompt and tools as the main
agent. The purpose is context isolation: keeping the main agent's tool call
history clean while delegating specific tasks to a fresh agent instance.

Use cases:
- Research tasks that would pollute the main conversation
- Investigating specific files or patterns in isolation
- Any focused work where you want fresh context
"""

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from langchain.tools import tool, ToolRuntime
from langchain_core.messages import ToolMessage, SystemMessage
from langgraph.types import Command
from datetime import date
import logging

# Import the same tools, state, and prompt used by the main agent
from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_ticket_mode_agent.prompts import TICKET_MODE_AGENT_PROMPT
from app.agents.leonardo.rails_agent.tools import (
    write_todos, ls, read_file, write_file, edit_file, search_file, bash_command
)

logger = logging.getLogger(__name__)


def get_cached_system_prompt():
    """Build system message with current date and prompt caching enabled.

    Date is appended at the end so the main prompt can be cached,
    and only the date portion changes daily.
    """
    current_date = date.today().strftime("%Y-%m-%d")
    prompt_with_date = f"{TICKET_MODE_AGENT_PROMPT}\n\n---\n**Today's Date:** {current_date}\n(Use this date when creating ticket filenames: {current_date}_TYPE_description.md)"

    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": prompt_with_date,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    )


# =============================================================================
# Sub-Agent Factory
# =============================================================================

def create_sub_agent():
    """Create a sub-agent instance with the same config as the main Ticket Mode agent.

    The sub-agent uses:
    - Same system prompt (TICKET_MODE_AGENT_PROMPT) with prompt caching
    - Same tools (ticket mode tools - no internet_search)
    - Same state schema (RailsAgentState)

    The only difference is it runs in isolated context without the main
    conversation's message history.

    Returns:
        A compiled LangGraph agent identical to the main Ticket Mode agent
    """
    # Same tools as the main Ticket Mode agent (minus delegate_task to prevent recursion)
    # Note: Ticket Mode doesn't have internet_search
    sub_agent_tools = [
        write_todos,
        ls, read_file, write_file, edit_file, search_file,
        bash_command,
        # Note: delegate_task is NOT included to prevent infinite recursion
    ]

    # Use the same model as the main agent's default
    model = ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)

    return create_agent(
        model=model,
        tools=sub_agent_tools,
        system_prompt=get_cached_system_prompt(),  # With prompt caching
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
    - Investigating specific files or patterns in isolation
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
            task_description="Research the User model in rails/app/models/user.rb. Look at its associations, callbacks, and validations. Summarize the key attributes and relationships."
        )

        delegate_task(
            task_description="Search for all Turbo Frame usages in rails/app/views/boqs/. List each frame ID and its purpose."
        )
    """
    tool_call_id = runtime.tool_call_id
    logger.info(f"Ticket Mode delegating task to sub-agent: {task_description[:100]}...")

    try:
        sub_agent = create_sub_agent()

        # Invoke with the task - sub-agent starts with fresh context
        result = sub_agent.invoke({
            "messages": [{"role": "user", "content": task_description}]
        })

        # Extract the final response from the sub-agent
        final_message = result["messages"][-1].content if result["messages"] else "No response from sub-agent"

        logger.info("Ticket Mode sub-agent completed task successfully")

        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"[DELEGATED TASK COMPLETED]\n\n{final_message}",
                    tool_call_id=tool_call_id
                )
            ]
        })

    except Exception as e:
        logger.error(f"Ticket Mode sub-agent failed: {str(e)}")
        return Command(update={
            "messages": [
                ToolMessage(
                    content=f"[DELEGATED TASK FAILED]\n\nError: {str(e)}",
                    tool_call_id=tool_call_id
                )
            ],
            "failed_tool_calls_count": 1  # Increment failure counter
        })
