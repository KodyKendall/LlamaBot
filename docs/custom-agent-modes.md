# Adding a custom agent mode (per-instance, no image rebuild)

A Leonardo instance can ship its **own** LangGraph agent mode — a hand-authored
`nodes.py` with its own system prompt, a curated tool set, and a model of its
choice — and have it appear as a selectable option in the chat.html mode
dropdown, **without rebuilding the `kody06/llamabot` image**.

This works because all three touchpoints that gate a mode are either per-instance
overlay files or driven by an optional overlay the image now reads at request
time. The image change (0.5.1g) made the dropdown render dynamically from a
per-instance overlay instead of a hardcoded list.

## The recipe — 3 edits + 1 restart

All paths are inside the instance's mounted LlamaBot overlay (next to the
editable `langgraph.json`). None of this requires an image rebuild.

### 1. Author the agent graph — `user_agents/<name>/nodes.py`

Write a `build_workflow` that returns a compiled LangGraph graph. The simplest
contract (the one `user_agents/leo` uses) accepts the **standard state** the
default Rails `AgentStateBuilder` already sends — `message`, `thread_id`,
`api_token`, `agent_prompt` — and carries its own system prompt + tools inside
`nodes.py`. With that contract you need **no per-agent Rails state builder**.

Tools call back into Rails (authenticated) with the existing helper:

```python
from app.agents.utils.make_api_request_to_llamapress import make_api_request_to_llamapress
# inside a tool:
make_api_request_to_llamapress("GET", "/research_requests/123",
                               api_token=state["api_token"])
```

> ⚠️ The graph's Pydantic state type must accept exactly what
> `AgentStateBuilder#build` sends. A mismatch surfaces as a silent Pydantic error
> inside a broken `ToolMessage`. Reusing the standard state (above) avoids this.

### 2. Register the graph — `langgraph.json`

```json
{
  "graphs": {
    "leo": "./user_agents/leo/nodes.py:build_workflow",
    "research_agent": "./user_agents/research_agent/nodes.py:build_workflow"
  }
}
```

### 3a. Allow routing — Rails `app/llama_bot/agent_state_builder.rb`

Add the graph name to the routing allowlist (per-instance overlay, hot-reloaded):

```ruby
ROUTABLE_AGENTS = %w[leo campaign_leo research_agent].freeze
```

An `agent_name` not in `ROUTABLE_AGENTS` safely collapses to `DEFAULT_AGENT`.

### 3b. Declare the dropdown entry — `agent_modes.json`

Create `agent_modes.json` **next to `langgraph.json`** in the overlay. It is a
JSON array of custom modes:

```json
[
  {
    "key": "research",
    "label": "Research Mode",
    "agent_name": "research_agent",
    "description": "Web research + contact enrichment",
    "shortLabel": "Research"
  }
]
```

| field         | required | notes |
|---------------|----------|-------|
| `key`         | yes      | dropdown option value; must not collide with a built-in mode |
| `label`       | yes      | text shown in the dropdown |
| `agent_name`  | yes      | **must** be a graph registered in `langgraph.json` |
| `description` | no       | free text |
| `shortLabel`  | no       | compact toolbar label (defaults to `label`) |
| `icon`        | no       | reserved for future use |

The backend validates this file on every chat page load:
- file missing / invalid JSON / not an array → ignored (stock behavior)
- an entry whose `agent_name` is **not** a registered graph is **dropped** (it
  could never route, so it must not appear as a dead option)
- an entry whose `key` shadows a **built-in** mode is dropped (built-ins win)
- duplicate keys → first wins

Valid entries are injected as `window.LLAMABOT_CUSTOM_AGENT_MODES`; the frontend
appends them to the dropdown and merges their `key → agent_name` mapping under
the built-ins. For an **engineer-role** user on the default visibility setting,
the new key is auto-added to `visible_agents` so the option shows. If an instance
uses an explicit per-user `visible_agents` setting, add the custom key to it
deliberately.

### 4. Restart

Graphs load at LlamaBot **boot**, so:

```bash
docker compose restart llamabot
```

The new mode now appears in the chat dropdown, is selectable, routes to your
graph, and the agent can call back into Rails using `api_token`.

## Built-in modes (cannot be shadowed)

`engineer`, `ai_builder`, `testing`, `ticket`, `user`, `beginner`, `pyxl`,
`plan`, `feedback`.

## Motivating example (not shipped in the image)

Instance `jeff-pollock-partners` is a real-estate CRM that needs **web research +
contact enrichment** (find missing emails/phones for `Contact` rows). The generic
database/user mode (text-only `deepseek-v4-flash`, no web tool) burns its budget
navigating the Rails app and never searches the web. A focused "Research" mode —
a capable model + `web_search`/`fetch_url` + a narrow Rails API tool to read
`ResearchRequest` leads and update `Contact` email/phone — just does it. That
agent's `nodes.py` is authored per-instance using this recipe; it is intentionally
**not** baked into the shared image.
