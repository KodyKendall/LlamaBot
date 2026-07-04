"""
PyXL Agent — Excel-to-Rails Tech Spec Mode.

Reverse-engineers an uploaded Excel workbook (saved to the shared Rails mount at
app/imports/) into a Rails application tech spec. Ported from the standalone
OpenPyXL Excel Analysis Agent and adapted to LlamaBot's conversational,
checkpointed, model-selectable agent architecture.

Uses create_agent with the same middleware stack as rails_agent so it respects
the user's model selection, summarizes long conversations, and shares the failure
circuit breaker.
"""

from langchain_google_genai import ChatGoogleGenerativeAI
from app.agents.leonardo.agent_factory import build_leonardo_agent
from langchain.agents.middleware import SummarizationMiddleware
from app.agents.leonardo.summarization import make_summarization_middleware
from langchain_core.messages import SystemMessage

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.rails_agent.tools import (
    write_todos,
    internet_search,
    save_memory, list_memories, delete_memory,
    build_use_skill_tool, list_skills, read_skill, write_skill, edit_skill, delete_skill,
)
from app.agents.leonardo.pyxl_agent.tools import (
    list_spreadsheets,
    list_sheets, read_headers, sample_rows, get_cell_value,
    get_sheet_dimensions, summarize_column, detect_column_types,
    find_patterns_and_anomalies, statistical_analysis,
    check_formulas, find_cross_sheet_relationships, data_quality_check,
    read_tech_spec, write_tech_spec,
)
from app.agents.leonardo.pyxl_agent.prompts import EXCEL_ANALYSIS_PROMPT
from app.agents.leonardo.project_context import resolve_base_prompt
from app.agents.leonardo.rails_agent.middleware import (
    check_failure_limit,
    DynamicModelMiddleware,
    deepseek_reasoning_fix,
)
from app.agents.utils.token_counter import gemini_multimodal_token_counter, SUMMARIZATION_TOKEN_THRESHOLD

import logging
logger = logging.getLogger(__name__)


def get_cached_system_prompt():
    """Build the system message with Anthropic prompt caching enabled."""
    base_prompt = resolve_base_prompt(EXCEL_ANALYSIS_PROMPT, agent_mode="pyxl_agent")
    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": base_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    )


SUMMARIZATION_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This agent (PyXL) reverse-engineers uploaded Excel workbooks (in app/imports/) into a Rails application tech spec. Keep track of which workbook is being analyzed, what has been discovered about its sheets/columns/formulas, and what the user wants in the spec.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points.

Your summary should include the following sections:

1. Primary Request and Intent: What the user wants analyzed and what kind of output they expect
2. Workbook Under Analysis: Which file_path(s) are being analyzed, and the key findings so far (sheets, models, critical formulas)
3. All user messages: List ALL user messages that are not tool results
4. Pending Tasks: Any outstanding requests
5. Current Work: What was being worked on immediately before this summary
6. Optional Next Step: The next analysis step or spec section to produce

# Conversation to summarize:
{messages}
"""


# Read-only OpenPyXL analysis tools + planning/research/memory helpers.
default_tools = [
    list_spreadsheets,
    list_sheets,
    read_headers,
    sample_rows,
    get_cell_value,
    get_sheet_dimensions,
    summarize_column,
    detect_column_types,
    find_patterns_and_anomalies,
    statistical_analysis,
    check_formulas,
    find_cross_sheet_relationships,
    data_quality_check,
    write_todos,
    internet_search,
    save_memory, list_memories, delete_memory,
    list_skills, read_skill, write_skill, edit_skill, delete_skill,  # Skill library
    read_tech_spec, write_tech_spec,
]


def build_workflow(checkpointer=None):
    """Build the PyXL Excel-analysis agent workflow with create_agent."""
    # Default to Gemini for its large context window (whole-workbook analysis).
    # DynamicModelMiddleware overrides this per-request from state['llm_model'],
    # so the user's model selection in the UI is still respected.
    default_model = ChatGoogleGenerativeAI(
        model="gemini-3-flash-preview",
        vertexai=False,
        temperature=0.2,
    )

    middleware = [
        # Summarization for long conversations (shared factory: provider fallback
        # model, token-budgeted keep, first-user-messages + todo preservation;
        # REMOVE_ALL honored by the DeltaChannel reducer).
        make_summarization_middleware(summary_prompt=SUMMARIZATION_PROMPT),
        DynamicModelMiddleware(),
        deepseek_reasoning_fix,
        check_failure_limit,
    ]

    return build_leonardo_agent(
        model=default_model,
        tools=[*default_tools, build_use_skill_tool()],
        system_prompt=get_cached_system_prompt(),
        state_schema=RailsAgentState,
        middleware=middleware,
        checkpointer=checkpointer,
    )
