# Research Memo: InjectedState Compatibility with LangChain 1.0 create_agent

## Executive Summary

We attempted to refactor a LangGraph agent from manual `StateGraph` construction to the new `create_agent` factory pattern introduced in LangChain 1.0. The refactoring is **blocked** because tools using `InjectedState` from `langgraph.prebuilt` fail with validation errors when used with `create_agent` from `langchain.agents`.

**Core Question**: Is `InjectedState` supported by `create_agent`? If not, what is the recommended pattern for accessing agent state within tools in LangChain 1.0?

---

## Problem Statement

### Current Architecture (Working)

```python
# Manual StateGraph with ToolNode - InjectedState works
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

builder = StateGraph(RailsAgentState)
builder.add_node("agent", agent_node)
builder.add_node("tools", ToolNode(tools))
# ... manual edges ...
graph = builder.compile(checkpointer=checkpointer)
```

Tools use `InjectedState` to access state:

```python
from langgraph.prebuilt import InjectedState
from typing import Annotated

@tool
def read_file(
    relative_path: str,
    state: Annotated[RailsAgentState, InjectedState],  # <-- State injected here
) -> str:
    """Read a file and return its contents."""
    rails_path = state.get("rails_path", "/app/app/rails")  # Access state fields
    # ...
```

### Attempted Refactoring (Broken)

```python
# New create_agent factory - InjectedState NOT working
from langchain.agents import create_agent

agent = create_agent(
    model=model,
    tools=default_tools,  # Same tools with InjectedState
    system_prompt=RAILS_AGENT_PROMPT,
    state_schema=RailsAgentState,  # Custom state schema provided
    middleware=[...],
    checkpointer=checkpointer,
)
```

### The Error

When any tool with `InjectedState` is invoked:

```
pydantic_core._pydantic_core.ValidationError: 1 validation error for read_file
state
  Field required [type=missing, input_value={'relative_path': 'app/mo...ead file contents...'}, input_data_json='{"relative_path":"app/mo...ead file contents..."}']
```

The `state` parameter is NOT being injected - it's being treated as a regular required parameter that the LLM must provide.

---

## Research Questions

### Primary Questions

1. **Does `create_agent` from `langchain.agents` support `InjectedState` from `langgraph.prebuilt`?**
   - If yes: What configuration is needed to enable it?
   - If no: Is this a known limitation or a bug?

2. **What is the recommended pattern for accessing agent state within tools in LangChain 1.0?**
   - Is there a new `langchain.agents` equivalent to `InjectedState`?
   - Has the pattern changed to something like `RunnableConfig` or context managers?

3. **Is `state_schema` parameter on `create_agent` supposed to enable `InjectedState` injection?**
   - We pass `state_schema=RailsAgentState` but state is still not injected into tools

### Secondary Questions

4. **Are there community discussions about this compatibility issue?**
   - GitHub issues on langchain or langgraph repos
   - Discord discussions
   - Stack Overflow questions

5. **What is the relationship between `langchain.agents` and `langgraph.prebuilt`?**
   - Are they designed to work together?
   - Is `create_agent` a wrapper around LangGraph primitives?
   - Does `create_agent` use `ToolNode` internally?

6. **What alternatives exist for state access in tools?**
   - `RunnableConfig` with `configurable` fields
   - Tool closures that capture state
   - Dependency injection patterns
   - Global/context-based state access

---

## Technical Context

### Package Versions

```
langchain >= 1.0.0
langgraph >= 1.0.0
langchain-anthropic (recent)
```

### Key Imports Currently Used

```python
# From langgraph (working with manual StateGraph)
from langgraph.prebuilt import InjectedState
from langgraph.prebuilt.chat_agent_executor import AgentState
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

# From langchain (new pattern - InjectedState broken)
from langchain.agents import create_agent, AgentState
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware
```

### Our Custom State Schema

```python
from langchain.agents import AgentState
from typing import NotRequired, Annotated, Any
import operator

class RailsAgentState(AgentState):
    todos: Annotated[NotRequired[list[Todo]], operator.add]
    debug_info: NotRequired[dict[str, Any]]
    agent_mode: NotRequired[str]
    llm_model: NotRequired[str]
    failed_tool_calls_count: Annotated[NotRequired[int], operator.add]
```

### Example Tool Using InjectedState

```python
@tool
def read_file(
    relative_path: str,
    state: Annotated[RailsAgentState, InjectedState],
) -> str:
    """Read a file within the Rails project and return its contents.

    Args:
        relative_path: Path relative to the Rails project root
    """
    rails_path = state.get("rails_path", "/app/app/rails")
    full_path = os.path.join(rails_path, relative_path)
    # ... file reading logic ...
```

The LLM is supposed to call this tool with just `relative_path`. The `state` parameter should be automatically injected by the agent runtime, NOT passed by the LLM.

---

## What We've Already Tried

### Attempt 1: Pass state_schema to create_agent

```python
agent = create_agent(
    model=model,
    tools=tools,
    state_schema=RailsAgentState,  # <-- Hoping this enables injection
    ...
)
```
**Result**: Same validation error. State not injected.

### Attempt 2: Use langchain.agents.AgentState as base

Changed from:
```python
from langgraph.prebuilt.chat_agent_executor import AgentState
```
To:
```python
from langchain.agents import AgentState
```
**Result**: Same validation error. State not injected.

### Attempt 3: Create duplicate tools without InjectedState for sub-agents

Created separate tool implementations that don't use `InjectedState` and hardcode paths.

**Result**: Works but defeats the purpose of state injection and creates maintenance burden.

---

## Hypotheses to Investigate

### Hypothesis A: create_agent doesn't use ToolNode internally

The `InjectedState` pattern might rely on `langgraph.prebuilt.ToolNode` specifically. If `create_agent` uses a different tool execution mechanism, it wouldn't know how to inject state.

**Research**: Check `create_agent` source code to see how it executes tools.

### Hypothesis B: InjectedState is a langgraph-only pattern

`InjectedState` is from `langgraph.prebuilt`, not `langchain`. The two packages might not be designed to interoperate at the tool injection level.

**Research**: Check if there's a `langchain.agents` equivalent to `InjectedState`.

### Hypothesis C: Different annotation pattern needed

Maybe LangChain 1.0 uses a different annotation pattern like:
```python
from langchain.tools import InjectedConfig  # hypothetical
state: Annotated[RailsAgentState, InjectedConfig("state")]
```

**Research**: Check LangChain 1.0 tool documentation for state/config injection patterns.

### Hypothesis D: RunnableConfig is the new pattern

LangChain has `RunnableConfig` with `configurable` fields. Maybe state access is now done via config:
```python
@tool
def read_file(relative_path: str, config: RunnableConfig) -> str:
    state = config.get("configurable", {}).get("state", {})
```

**Research**: Check if RunnableConfig is how state is accessed in create_agent tools.

### Hypothesis E: Bug or oversight in LangChain 1.0

The `create_agent` function might simply not have implemented `InjectedState` support yet, and it's a missing feature.

**Research**: Check GitHub issues for bug reports about this.

---

## Potential Solutions (To Validate)

### Solution 1: Revert to manual StateGraph

If `create_agent` fundamentally doesn't support `InjectedState`, we may need to keep using the manual `StateGraph` pattern and abandon the middleware refactoring.

### Solution 2: Tool closures

Wrap tools in closures that capture state from the outer scope:

```python
def make_tools(state):
    @tool
    def read_file(relative_path: str) -> str:
        rails_path = state.get("rails_path", "/app/app/rails")
        # ...
    return [read_file, ...]
```

**Drawback**: State would be captured at agent creation time, not at tool execution time.

### Solution 3: RunnableConfig pattern

If this is the new pattern, refactor tools to use config injection:

```python
from langchain_core.runnables import RunnableConfig

@tool
def read_file(relative_path: str, config: RunnableConfig) -> str:
    state = config.configurable.get("state", {})
    # ...
```

### Solution 4: Use langgraph's create_react_agent instead

`langgraph.prebuilt.create_react_agent` might support `InjectedState` since it's from the same package. Check if middleware can be added to it.

```python
from langgraph.prebuilt import create_react_agent

agent = create_react_agent(
    model=model,
    tools=tools,
    state_schema=RailsAgentState,
    checkpointer=checkpointer,
)
```

---

## Resources to Check

### Official Documentation
- LangChain 1.0 release notes and migration guide
- LangChain agents documentation (new create_agent API)
- LangGraph prebuilt agents documentation
- Tool injection patterns in LangChain 1.0

### Source Code
- `langchain.agents.create_agent` implementation
- `langgraph.prebuilt.ToolNode` implementation
- `langgraph.prebuilt.InjectedState` implementation
- How `state_schema` is used in both packages

### Community Resources
- GitHub issues on langchain-ai/langchain repo (search: "InjectedState", "create_agent", "state injection")
- GitHub issues on langchain-ai/langgraph repo
- LangChain Discord #help channel
- Stack Overflow questions about LangChain 1.0 tool patterns

### Blog Posts / Announcements
- LangChain 1.0 blog post (https://blog.langchain.com/langchain-langgraph-1dot0/)
- Agent middleware blog post (https://blog.langchain.com/agent-middleware/)
- Any posts about tool state injection patterns

---

## Expected Deliverables

After research, provide:

1. **Definitive answer**: Does `create_agent` support `InjectedState`? Yes/No/Partially

2. **If No**: What is the official recommended pattern for state access in tools with `create_agent`?

3. **Migration path**: Concrete code examples showing how to refactor tools that currently use `InjectedState` to work with `create_agent`

4. **Trade-offs**: If we must change patterns, what are the trade-offs vs. keeping manual StateGraph?

5. **Community status**: Are others facing this issue? Is it being addressed? ETA for fix if it's a bug?

---

## Files in Our Codebase

For context, here are the key files in our project:

| File | Description |
|------|-------------|
| `app/agents/leonardo/rails_agent/nodes.py` | Main agent definition (refactored to use create_agent) |
| `app/agents/leonardo/rails_agent/tools.py` | All tools using InjectedState (~1300 lines) |
| `app/agents/leonardo/rails_agent/state.py` | Custom RailsAgentState schema |
| `app/agents/leonardo/rails_agent/middleware.py` | New middleware for create_agent |
| `app/agents/leonardo/rails_agent/sub_agents.py` | Sub-agent spawning with delegate_task |

---

## Priority

**HIGH** - This is blocking our entire middleware refactoring effort. We need clarity on whether to:
1. Find a fix and continue with `create_agent`
2. Abandon `create_agent` and revert to manual `StateGraph`
3. Refactor all 12+ tools to use a different state access pattern

---

*Memo prepared for deep research agent investigation*
*Date: 2025-12-13*
