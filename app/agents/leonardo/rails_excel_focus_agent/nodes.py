"""
Excel Focus Agent — One Page Focus Mode.

A highly constrained agent that only edits app/views/public/welcome.html.erb.
Designed for non-technical users iterating on a single-page layout with
Daisy UI, Tailwind, and Font Awesome.

Uses create_agent with the same middleware stack as rails_agent.
"""

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import SystemMessage

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos, read_file, write_file, edit_file,
    internet_search,
    save_memory, list_memories, delete_memory,
    read_leonardo_md, edit_leonardo_md, write_leonardo_md,
)
from app.agents.leonardo.rails_excel_focus_agent.prompts import FOCUSED_MODE_PROMPT
from app.agents.leonardo.project_context import build_beginner_system_prompt
from app.agents.leonardo.rails_agent.middleware import (
    inject_view_context,
    check_failure_limit,
    DynamicModelMiddleware,
    deepseek_reasoning_fix,
)
from app.agents.utils.token_counter import gemini_multimodal_token_counter, SUMMARIZATION_TOKEN_THRESHOLD

import logging
logger = logging.getLogger(__name__)


def get_cached_system_prompt():
    """Build system message with beginner personality context and prompt caching."""
    full_prompt = build_beginner_system_prompt(FOCUSED_MODE_PROMPT)
    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": full_prompt,
                "cache_control": {"type": "ephemeral"}
            }
        ]
    )


SUMMARIZATION_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This agent is in One Page Focus Mode — it can ONLY edit app/views/public/welcome.html.erb. Keep track of what was built on that page and what the user wants next.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points.

Your summary should include the following sections:

1. Primary Request and Intent: What the user wants on their page
2. Current Page State: What is currently on app/views/public/welcome.html.erb — layout, components, sections
3. All user messages: List ALL user messages that are not tool results
4. Pending Tasks: Any outstanding requests
5. Current Work: What was being worked on immediately before this summary
6. Optional Next Step: The next thing to build on the page

# Conversation to summarize:
{messages}
"""


# Minimal tool set — no bash, no glob/grep, no git, no delegation
default_tools = [
    write_todos,
    read_file, write_file, edit_file,
    internet_search,
    save_memory, list_memories, delete_memory,
    read_leonardo_md, edit_leonardo_md, write_leonardo_md,
]


def build_workflow(checkpointer=None):
    """Build the Excel Focus agent workflow with create_agent."""
    default_model = ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)

    summarization_model = ChatGoogleGenerativeAI(
        model="gemini-3-flash-preview",
        vertexai=False,
        temperature=1.0,
    )
    middleware = [
        SummarizationMiddleware(
            model=summarization_model,
            trigger=("tokens", SUMMARIZATION_TOKEN_THRESHOLD),
            keep=("messages", 15),
            token_counter=gemini_multimodal_token_counter,
            trim_tokens_to_summarize=None,
            summary_prompt=SUMMARIZATION_PROMPT,
        ),
        DynamicModelMiddleware(),
        deepseek_reasoning_fix,
        inject_view_context,
        check_failure_limit,
    ]

    return create_agent(
        model=default_model,
        tools=default_tools,
        system_prompt=get_cached_system_prompt(),
        state_schema=RailsAgentState,
        middleware=middleware,
        checkpointer=checkpointer,
    )
