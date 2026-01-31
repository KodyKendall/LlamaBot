"""
Sub-Agent Spawning for Rails Ticket Mode Agent.

This module provides the `delegate_task` tool that allows the main Ticket Mode agent
to spawn sub-agents for focused RESEARCH work with isolated context.

IMPORTANT: Sub-agents are RESEARCH-ONLY. They do NOT create tickets.
The main agent is responsible for calling write_final_ticket after receiving research.

Use cases:
- Technical research on the Rails codebase
- Investigating specific files or patterns in isolation
- Database schema analysis
- Model/controller/view exploration
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
You are delegated focused research tasks by the main Ticket Mode agent.
Your job is to investigate the codebase and RETURN your findings.

## CRITICAL RULES
1. **DO NOT create tickets** - The main agent handles ticket creation
2. **DO NOT write files** - You are read-only
3. **DO NOT announce ticket creation** - You only do research
4. **RETURN structured findings** - The main agent needs your research to create the ticket

## RESEARCH METHODOLOGY

### Step 1: Root Cause Layer Classification
Classify where the problem originates:
| Layer | What to Check | Example Issues |
|-------|---------------|----------------|
| **DB (Seeds/Migrations)** | `db/seeds.rb`, `db/migrate/*.rb` | Non-idempotent seeds, bad backfills |
| **Model (Callbacks)** | `after_create`, `after_save`, `after_commit` | Auto-creating related records |
| **Controller (Query)** | Scopes, `.where()`, `.find_by()` | Wrong records fetched |
| **View (Display)** | Partials, templates, Turbo frames | Rendering issues |

### Step 2: Five Whys Analysis
Write a 5-step "Why?" chain with layer-tagged hypotheses, then verify with evidence.

### Step 3: Technical Research
Build a complete technical mental model:
1. Database tables and columns (start with rails/db/schema.rb)
2. ActiveRecord associations and callbacks (rails/app/models/)
3. Business logic in models/controllers (rails/app/controllers/)
4. UI components - views, partials, Turbo frames (rails/app/views/)
5. Stimulus controllers (rails/app/javascript/controllers/)
6. Routes (rails/config/routes.rb)

## OUTPUT FORMAT

Return your findings in this structured format:

### Root Cause Classification
- **Primary layer:** [DB | Model | Controller | Views]
- **Secondary layers (if any):** [list]
- **Is this a DATA problem or DISPLAY problem?** [answer]
- **Evidence:** [file paths + brief explanation]

### Five Whys Analysis
**Hypothesis Pass:**
- Why #1: [symptom] → Hypothesis: [guess] (Layer: X)
- Why #2-5: ...

**Evidence Pass:**
- Why #1: [symptom] → Evidence: [file:line]
- Why #2-5: ...

### Database Schema
**Relevant Tables:**
| Table | Key Columns | Purpose |

### Models & Associations
**Model: ModelName**
- Location: path
- Associations: list
- Key Callbacks: list

### Controllers & Routes
**Controller: ControllerName**
- Location: path
- Relevant Actions: list

### UI Components
**Views/Partials:** list with paths
**Turbo Frames:** frame IDs and purposes
**Stimulus Controllers:** controller names and purposes

### Business Logic Summary
[How the pieces connect - what triggers what, data flow]

### Code Health Observations
| Severity | Category | Location | Description |
|----------|----------|----------|-------------|
| CRITICAL/MEDIUM/LOW | [category] | file:line | [description] |

### Implementation Considerations
[Any gotchas, edge cases, or suggestions for the engineer]

---
**Remember: RETURN your findings. DO NOT create tickets or write files.**
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
    - Research-only system prompt (NOT the full ticket mode prompt)
    - Read-only tools (no write_file, no edit_file)
    - Same LLM model as the main agent (passed from state.llm_model)

    IMPORTANT: Sub-agents do NOT create tickets. They only research and return findings.
    The main agent is responsible for calling write_final_ticket.

    Args:
        llm_model: The model name from state.llm_model (e.g., 'claude-4.5-haiku', 'gemini-3-flash').
                   If None, defaults to 'gemini-3-flash'.

    Returns:
        A compiled LangGraph research agent
    """
    # RESEARCH-ONLY tools - no write_file, no edit_file
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

    IMPORTANT: The sub-agent is RESEARCH-ONLY. It will NOT create tickets.
    After receiving the research findings, YOU (the main agent) must call
    write_final_ticket to create the ticket in the database.

    When to use:
    - Technical research on the Rails codebase
    - Investigating database schema, models, controllers, views
    - Root cause analysis (Five Whys)
    - Code health observation gathering

    Args:
        task_description: Clear, detailed description of what needs to be researched.
            Include: URL, User Story, Current Behavior, Desired Behavior,
            Verification Criteria, and any Business Rules from the observation.

    Returns:
        Structured research findings including:
        - Root Cause Classification
        - Five Whys Analysis
        - Database Schema
        - Models & Associations
        - Controllers & Routes
        - UI Components
        - Code Health Observations
        - Implementation Considerations


    
    Example:
        delegate_task(
            task_description="Research the User model in rails/app/models/user.rb. Look at its associations, callbacks, and validations. Summarize the key attributes and relationships."
        )

        delegate_task(
            task_description="Search for all Turbo Frame usages in rails/app/views/boqs/. List each frame ID and its purpose."
        )

    After receiving findings, YOU must call write_final_ticket to create the ticket.
    """
    tool_call_id = runtime.tool_call_id
    # Get the llm_model from the main agent's state so sub-agent uses the same model
    llm_model = runtime.state.get('llm_model')
    logger.info(f"Ticket Mode delegating task to sub-agent (model: {llm_model}): {task_description[:100]}...")

    try:
        sub_agent = create_sub_agent(llm_model=llm_model)

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
