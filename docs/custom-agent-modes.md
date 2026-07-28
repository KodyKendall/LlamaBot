# Adding a custom agent mode (per-instance, no image rebuild)

A Leonardo instance can ship its **own** LangGraph agent mode — a hand-authored
`nodes.py` with its own system prompt, a curated tool set, and a model of its
choice — and have it appear as a selectable option in the chat.html mode
dropdown, **without rebuilding the `kody06/llamabot` image**.

> **Handing this to an agent?** Use `docs/cookbooks/register-custom-leo-mode.md`
> — a self-contained, drop-into-any-session version of this recipe with
> environment discovery and headless verification steps. Keep the two in sync.

Everything that gates a mode is a per-instance overlay file read at request
time. The agent registry is layered (platform base baked into the image +
client overlay on a host mount, merged by `app/lib/langgraph_registry.py`) —
see `docs/dev/langgraph_registry_layering.md` for the design. **Never register
client agents in the base `langgraph.json`**; the platform deliberately ships
zero client agents.

## The recipe — 3 edits

On a Leonardo instance the files live in `langgraph/` and are mounted into the
llamabot container by compose:

```
./langgraph/agents                → /app/app/user_agents           # your agent code
./langgraph/langgraph.local.json  → /app/app/langgraph.local.json  # graph registry overlay
./langgraph/agent_modes.json      → /app/app/agent_modes.json      # chat dropdown entries
```

### 1. Author the agent graph — `langgraph/agents/<name>/nodes.py`

Expose `build_workflow(checkpointer=None)` returning a compiled StateGraph.
Platform code is importable via the `app.` package (LlamaBot's `app/` is on the
path in-container). Always get models via the factory — never hardcode a
provider class:

```python
from app.agents.leonardo.llm_factory import get_llm
```

The simplest contract accepts the standard state the default Rails
`AgentStateBuilder` already sends — `message`, `thread_id`, `api_token`,
`agent_prompt` — and carries its own system prompt + tools inside `nodes.py`.
With that contract you need no per-agent Rails state builder. The reference
implementation is `langgraph/agents/leo/nodes.py` in Leonardo.

### 2. Register the graph — `langgraph.local.json`

```json
{
  "graphs": {
    "research_agent": "./user_agents/research_agent/nodes.py:build_workflow"
  }
}
```

The path is container-relative: `./user_agents/...`, **not**
`./langgraph/agents/...`. Overlay keys win on collision with the base, so an
instance can also deliberately shadow a platform agent. (The AI-builder's
`edit_langgraph_json` tool writes to this same overlay.)

### 3. Declare the dropdown entry — `agent_modes.json`

A JSON array of custom modes, next to `langgraph.local.json`:

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
| `agent_name`  | yes      | **must** be a graph in the merged registry |
| `description` | no       | free text |
| `shortLabel`  | no       | compact toolbar label (defaults to `label`) |
| `icon`        | no       | reserved for future use |

The backend (`load_custom_agent_modes` in `app/routers/ui.py`) validates this
file on every chat page load:

- file missing / invalid JSON / not an array → ignored (stock behavior)
- an entry whose `agent_name` is **not** a registered graph is **dropped** (it
  could never route, so it must not appear as a dead option)
- an entry whose `key` shadows a **built-in** mode is dropped (built-ins win)
- duplicate keys → first wins

Valid entries are injected as `window.LLAMABOT_CUSTOM_AGENT_MODES`; the
frontend appends them to the dropdown and merges their `key → agent_name`
mapping under the built-ins.

## Restarts (mostly not needed)

- **New agent / new mode entry:** no restart. `agent_modes.json` is re-read on
  every chat page load, and an unknown graph is imported and compiled
  on-demand at the first message.
- **Editing an existing agent's Python:** restart required — the module and
  compiled graph are cached in-process (the backend runs without hot-reload):
  `docker compose restart llamabot`.

## Permissions

Mode visibility is role-based (`app/permissions.py`, stored in the
`role_agent_modes` SiteSetting). Custom modes ride along with a role's
**default** grant — e.g. an engineer-role user sees a new custom mode
immediately. If an admin has explicitly configured a role's mode list, the
custom key must be opted in there or that role won't see it.

## Built-in mode keys (cannot be shadowed)

`BUILTIN_AGENT_MODE_KEYS` in `app/permissions.py`, currently: `engineer`,
`ai_builder`, `testing`, `ticket`, `database`, `beginner`, `pyxl`, `plan`,
`chat`.

## Calling back into Rails as the signed-in user

Use the `llamapress_api` library (`app/lib/llamapress_api.py`) — exactly two
imports, and this import path is the stability contract (pinned by
`app/tests/test_llamapress_api.py`):

```python
from app.lib.llamapress_api import LlamaPressAPIState, rails_api_request

class MyState(LlamaPressAPIState):   # REQUIRED — puts api_token on the state
    ...                              # schema; LangGraph drops undeclared frame
                                     # fields, so skipping this silently
                                     # disables the tool (it fails closed).

tools = [rails_api_request]
```

The tool sends the frontend-minted, user-scoped token as
`Authorization: LlamaBot <token>`; the Rails gem verifies it and signs that
user in, so the app's own gates apply (`llama_bot_allow` allowlist, then
authorization). Deliberate constraints — don't route around them: only paths
under `/api/`, no redirects, no other hosts, and **a 403 is a correct answer**
(prompt the agent to report it, not work around it).

Don't put `rails_api_request` in an agent alongside unbounded tools
(`bash_command` etc.) — that hands the mode's users the Rails console.

Full auth flow, endpoint recipe, and verified attack table:
`docs/dev/user_api_mode.md`.

## Page context, current user, and browser control

Custom modes can use the same context plumbing built-in modes get: the
per-message `debug_info` frame field (current `request_path`, `view_path`,
`full_html`) by declaring it on the state schema; the signed-in user via a
small allowlisted `/api/me` endpoint; and the live-browser tools
(`navigate_browser`, `execute_browser_js`, `get_browser_js_logs` — gated by
the `enable_live_browser_tools` site setting, default off). Full recipes with
code: `docs/cookbooks/register-custom-leo-mode.md`, section "page context,
current user, and browser control".

## Motivating example (not shipped in the image)

A real-estate CRM instance needs **web research + contact enrichment** (find
missing emails/phones for `Contact` rows). The generic database mode (text-only
model, no web tool) burns its budget navigating the Rails app and never
searches the web. A focused "Research" mode — a capable model +
`web_search`/`fetch_url` + a narrow Rails API tool to read leads and update
`Contact` email/phone — just does it. That agent's `nodes.py` is authored
per-instance using this recipe; it is intentionally **not** baked into the
shared image.
