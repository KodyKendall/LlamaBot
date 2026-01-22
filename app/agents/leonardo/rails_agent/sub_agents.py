"""
Sub-Agent Spawning for Rails Agent.

This module provides the `delegate_task` tool that allows the main Rails agent
to spawn sub-agents for focused work with isolated context.

Sub-agents are NOT specialized - they use the same prompt and tools as the main
agent. The purpose is context isolation: keeping the main agent's tool call
history clean while delegating specific tasks to a fresh agent instance.

Use cases:
- Research tasks that would pollute the main conversation
- Implementing a specific feature in isolation
- Any focused work where you want fresh context
"""

from langchain.agents import create_agent
from langchain.tools import tool, ToolRuntime
from langchain_core.messages import ToolMessage, SystemMessage
from langgraph.types import Command
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
