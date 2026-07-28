# Cookbook: register a custom Leo agent mode on a Leonardo instance

**Audience:** an AI agent (Claude Code or similar) with shell + docker access on a
box running the Leonardo stack. This document is self-contained — you do not need
the LlamaBot or Leonardo source repos checked out to follow it.

**Mission:** add a new custom agent mode — its own LangGraph agent with its own
system prompt, tools, and model — and make it selectable in the chat mode
dropdown. No image rebuild. Three file edits, mostly no restart.

**Architecture in one paragraph:** LlamaBot is the platform (FastAPI + LangGraph
runtime, ships as the `kody06/llamabot` image). Leonardo is the client repo on the
instance; its `langgraph/` directory is mounted into the llamabot container. The
agent registry is layered: a platform base `langgraph.json` baked into the image,
plus a client overlay `langgraph.local.json` on a host mount, deep-merged at read
time (overlay wins). Custom modes live entirely in the mounted client files.

---

## Phase 0 — discover and verify the environment

Find the Leonardo checkout and compose project (commonly `~/Leonardo` or
`~/dev/Leonardo`):

```bash
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i llamabot   # find the container
docker inspect <llamabot-container> --format '{{json .Mounts}}' | python3 -m json.tool
```

Verify all three client mounts exist (destinations, inside the container):

| Host (Leonardo repo)               | Container                        | Purpose |
|------------------------------------|----------------------------------|---------|
| `langgraph/agents/`                | `/app/app/user_agents`           | your agent code |
| `langgraph/langgraph.local.json`   | `/app/app/langgraph.local.json`  | graph registry overlay |
| `langgraph/agent_modes.json`       | `/app/app/agent_modes.json`      | dropdown entries |

Also confirm the image supports the layered registry:

```bash
docker exec <llamabot-container> python -c "from app.lib.langgraph_registry import load_graphs; print(sorted(load_graphs()))"
```

**If a mount is missing** (older compose file): add the missing volume lines to the
llamabot service in the instance's `docker-compose.yml`, create the host files
(`langgraph.local.json` → `{"graphs": {}}`, `agent_modes.json` → `[]`) *before*
recreating (a missing host file would be created as a directory), then
`docker compose up -d --force-recreate llamabot`.

**If `langgraph_registry` doesn't import:** the image predates the layered
registry — stop and report; the instance needs an image update first.

> ⚠️ From here on, work only in the Leonardo `langgraph/` directory on the host.
> **Never edit the base `/app/app/langgraph.json` inside the container** — it is
> platform-owned, read-only in spirit, and overwritten by image updates.

---

## Phase 1 — author the agent: `langgraph/agents/<name>/nodes.py`

Contract: expose `build_workflow(checkpointer=None)` returning a compiled
StateGraph. Platform code imports via the `app.` package (LlamaBot's `app/` is on
the path in-container). The state must extend the platform's
`LlamaPressAPIState` if the agent calls back into Rails (see Phase 5), and should
accept the standard fields the Rails frontend sends: `message`, `thread_id`,
`api_token`, `agent_prompt`.

Known-good minimal template (mirror of the shipped `leo` reference agent):

```python
from langchain_core.messages import SystemMessage
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

# ALWAYS get models via the factory — never hardcode ChatOpenAI/Gemini/etc.
from app.agents.leonardo.llm_factory import get_llm

# Authenticated HTTP into the Rails app AS THE SIGNED-IN USER. Rails verifies the
# per-user token and applies its own permission gates; LlamaBot cannot forge one.
from app.lib.llamapress_api import LlamaPressAPIState, rails_api_request

# Every tool here is bounded only by what it does itself. rails_api_request is
# bounded by the user's Rails permissions; a bash/exec tool would NOT be, and
# would hand whoever can select this mode the Rails console. Add deliberately.
tools = [rails_api_request]

SYS_MSG = """You are <persona / job description here>.

You can call this app's JSON API with the `rails_api_request` tool. It runs as
the signed-in user, with their permissions — a 403 is a correct answer: report
it plainly rather than looking for another route. Only /api/ paths are reachable.

Available endpoints:
  GET  /api/...
"""

# REQUIRED subclass: puts api_token on the state schema. LangGraph drops
# undeclared frame fields, so plain MessagesState silently disables the tool.
class AgentModeState(LlamaPressAPIState):
    agent_prompt: str

def assistant(state: AgentModeState):
    llm = get_llm("deepseek-v4-flash")   # or another key llm_factory supports
    llm_with_tools = llm.bind_tools(tools)
    extra = state.get("agent_prompt") or ""
    sys = SystemMessage(content=f"{SYS_MSG}\n<DEVELOPER_INSTRUCTIONS>{extra}</DEVELOPER_INSTRUCTIONS>")
    return {"messages": [llm_with_tools.invoke([sys] + state["messages"])]}

def build_workflow(checkpointer=None):
    builder = StateGraph(AgentModeState)
    builder.add_node("assistant", assistant)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "assistant")
    builder.add_conditional_edges("assistant", tools_condition)
    builder.add_edge("tools", "assistant")
    return builder.compile(checkpointer=checkpointer)
```

The `app.lib.llamapress_api` import path is the stability contract — never import
Rails-auth helpers from deeper platform paths.

---

## Phase 2 — register the graph: `langgraph/langgraph.local.json`

```json
{
  "graphs": {
    "<agent_name>": "./user_agents/<name>/nodes.py:build_workflow"
  }
}
```

The path is **container-relative**: `./user_agents/...`, NOT
`./langgraph/agents/...`. Overlay keys win on collision, so a client can also
deliberately shadow a platform agent — don't do it by accident.

---

## Phase 3 — declare the dropdown entry: `langgraph/agent_modes.json`

A JSON **array**:

```json
[
  {
    "key": "<mode_key>",
    "label": "My Mode",
    "agent_name": "<agent_name>",
    "description": "What this mode is for",
    "shortLabel": "MyMode"
  }
]
```

Rules (enforced server-side on every chat page load; violations are silently
dropped from the dropdown):

- `key`, `label`, `agent_name` required, non-empty strings.
- `agent_name` must match a registered graph (Phase 2) exactly.
- `key` must not shadow a built-in mode key. Check the live set:
  ```bash
  docker exec <llamabot-container> python -c "from app.permissions import BUILTIN_AGENT_MODE_KEYS; print(sorted(BUILTIN_AGENT_MODE_KEYS))"
  ```
- Duplicate keys: first wins.

**Permissions:** custom modes ride along with a role's *default* mode grant (an
engineer-role user sees them immediately). If an admin explicitly configured a
role's mode list (the `role_agent_modes` SiteSetting), the new key must be opted
in there or that role won't see it.

---

## Phase 4 — restart (usually not needed)

- **New agent + new mode entry:** no restart. `agent_modes.json` is re-read per
  page load; an unknown graph compiles on-demand at the first message.
- **Editing an existing agent's Python:** restart required (modules and compiled
  graphs are cached in-process; the backend has no hot-reload):
  `docker compose restart llamabot`.
- **Compose/env changes:** `docker compose up -d --force-recreate llamabot`
  (plain `restart` does not reload env or mounts).

---

## Phase 5 — verify (headless, no browser needed)

**1. Graph registers and builds** (run after every nodes.py edit):

```bash
docker exec <llamabot-container> python -c "
from app.lib.langgraph_registry import load_graphs
graphs = load_graphs(); assert '<agent_name>' in graphs, graphs
import importlib.util
spec = importlib.util.spec_from_file_location('m', '/app/app/user_agents/<name>/nodes.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print('builds:', m.build_workflow() is not None)"
```

**2. Mode entry survives validation** (a dropped entry = dropdown won't show it):

```bash
docker exec <llamabot-container> python -c "
from app.routers.ui import load_custom_agent_modes
from app.lib.langgraph_registry import load_graphs
print(load_custom_agent_modes('/app/app/agent_modes.json', load_graphs()))"
```

Expected: your entry in the list. Empty list → check the rules in Phase 3 and
the container logs for a `Dropping custom agent mode` warning.

**3. Live Rails call as a real user** (only if the agent uses
`rails_api_request`) — mint a genuine token in the Rails container, call
through the tool:

```bash
TOKEN=$(docker compose exec -T llamapress sh -c 'cd /rails && bundle exec rails runner \
  "puts Rails.application.message_verifier(:llamabot_ws).generate({session_id: SecureRandom.uuid, user_id: User.first.id}, expires_in: 30.minutes)"' | tail -1)
docker exec -e T="$TOKEN" <llamabot-container> python -c "
import os
from app.lib.llamapress_api import rails_api_request
class R: state={'api_token': os.environ['T']}; tool_call_id='t'
print(rails_api_request.func(method='GET', path='/api/users', runtime=R())[:200])"
```

Expected `HTTP 200` + JSON. Decode table:

| Result | Meaning |
|---|---|
| 302 to /login | token not authenticating (expired, or wrong secret) |
| `401 LLAMA_AUTH_004` | bad token signature |
| 403 | authenticated but not allowlisted/authorized — often correct behavior |
| 404 | route missing, or path not under `/api/` (the tool only allows `/api/`) |

**4. End-to-end:** open the chat UI as an engineer-role user → the mode appears
in the dropdown → send a message → it routes to your graph.

---

## Optional — page context, current user, and browser control

Built-in modes know what page the user is on and can drive the browser. A custom
mode gets all of the same plumbing — none of it is reserved for built-ins.

### A. Current page context (`debug_info`)

The chat frontend asks the Rails iframe for page context and attaches it to
**every** WebSocket message, in every mode. The payload:

```json
{
  "request_path":  "/contacts/4",                      // current page URL path
  "view_path":     "app/views/contacts/show.html.erb", // ERB file rendering it
  "full_html":     "<html>…</html>",                   // the page's full HTML
  "page_loaded_at": "…"
}
```

The request handler copies frame fields into graph state **only if the state
schema declares them** — so opt in by declaring the field, then inject it into
the system message each turn:

```python
from typing import Any
from typing_extensions import NotRequired

class AgentModeState(LlamaPressAPIState):
    agent_prompt: str
    debug_info: NotRequired[dict[str, Any]]

def assistant(state: AgentModeState):
    llm_with_tools = get_llm("deepseek-v4-flash").bind_tools(tools)
    extra = state.get("agent_prompt") or ""
    debug = state.get("debug_info") or {}
    page_ctx = ""
    if debug.get("request_path"):
        page_ctx = (f'\nThe user is currently viewing page "{debug["request_path"]}"'
                    f' (rendered by "{debug.get("view_path", "unknown")}").')
    sys = SystemMessage(content=f"{SYS_MSG}{page_ctx}\n<DEVELOPER_INSTRUCTIONS>{extra}</DEVELOPER_INSTRUCTIONS>")
    return {"messages": [llm_with_tools.invoke([sys] + state["messages"])]}
```

Notes:

- This mirrors what built-in modes do (`ViewPathContextMiddleware` in platform
  code emits `<CONTEXT page="…" file="…"/>`); a raw StateGraph just does it
  inline.
- `full_html` is the entire rendered page — often 100KB+. Don't dump it into
  the prompt wholesale; omit it, truncate it, or include it only when the
  user's question is about the page content.
- `debug_info` can be absent (iframe not loaded yet, or the Rails page missed
  the context partial) — always code the `or {}` fallback.

### B. Current signed-in user

`debug_info` does not carry the user — but the agent already *is* the user:
`rails_api_request` sends their user-scoped token. Expose a minimal identity
endpoint in the Rails app (follows the endpoint recipe below):

```ruby
# config/routes.rb, inside `namespace :api`
get "me", to: "me#show"

# app/controllers/api/me_controller.rb
class Api::MeController < ApplicationController
  include LlamaBotRails::AgentAuth
  llama_bot_allow :show

  def show
    render json: current_user.slice(:id, :email, :name)
  end
end
```

Then tell the mode about it in its system prompt:

```
GET /api/me → the currently signed-in user (id, email, name)
```

Return only what the mode needs — id/email/name, not the whole user record.

### C. Browser control (navigate, run JS, read console)

Three platform tools drive the user's **live browser tab** (the Rails iframe in
the chat UI). Import them; don't reimplement:

```python
from app.agents.leonardo.rails_agent.tools import (
    navigate_browser,        # navigate the iframe to a path
    get_browser_js_logs,     # fetch (and clear) captured JS console logs
    execute_browser_js,      # run arbitrary JS in the page, return the result
    live_browser_tools_enabled,
)

def _build_tools():
    t = [rails_api_request]
    if live_browser_tools_enabled():   # honor the instance-wide gate — never bypass
        t.extend([navigate_browser, get_browser_js_logs])
        # execute_browser_js: add ONLY if this mode truly needs arbitrary JS —
        # it runs code in the signed-in user's session.
    return t

def build_workflow(checkpointer=None):
    tools = _build_tools()
    builder = StateGraph(AgentModeState)
    builder.add_node("assistant", make_assistant(tools))
    builder.add_node("tools", ToolNode(tools))
    ...
    return builder.compile(checkpointer=checkpointer)
```

How they work (so you can debug them): the tool fires a LangGraph interrupt →
the platform forwards it to the chat frontend as a `browser_command` WS frame →
the frontend executes it against the Rails iframe → the result resumes the
graph. Consequences:

- **They need the checkpointer.** Interrupt/resume persists state between the
  two halves — always forward the `checkpointer` argument to `.compile()` (the
  Phase 1 template already does).
- **Gated by the `enable_live_browser_tools` site setting, default off.** The
  setting is read when the tool list is built (workflow compile time), so after
  flipping it: `docker compose restart llamabot`.
- **They only work with a live chat tab.** In a headless/background run there
  is no browser to answer, so keep prompts honest: the tool may fail, and the
  agent should say so rather than retry forever.
- Threat model: `navigate_browser` is tame; `execute_browser_js` is arbitrary
  code in the user's authenticated session — treat adding it like adding a
  bash tool, and leave it out unless the mode's job requires it.

## Optional — expose a new Rails endpoint for the agent

In the instance's Rails app, under the `/api/` namespace:

1. Route in `config/routes.rb`, controller in `app/controllers/api/`.
2. `include LlamaBotRails::AgentAuth` + `llama_bot_allow :index, :show, ...` —
   only allowlisted actions are reachable with a token; the rest 403.
3. CSRF for token POSTs (Rails only exempts `Bearer`, not the `LlamaBot`
   scheme) — per-controller, don't blanket-skip:
   ```ruby
   skip_before_action :verify_authenticity_token
   before_action :verify_authenticity_token, unless: -> { llama_bot_request? || api_request? }
   ```
4. Strong params are the escalation gate — never permit `:admin` etc.

---

## Safety rails (non-negotiable)

- Never edit the platform base `langgraph.json` (in-container or in the image).
- Never pair `rails_api_request` with unbounded tools (`bash_command`, raw
  `exec`, …) in the same agent — that hands the mode's users the Rails console.
- The tool's constraints (`/api/` only, no redirects, no other hosts) are
  deliberate; a 403 is a correct answer. Don't route around any of them.
- All model selection through `get_llm(...)`; never hardcode a provider class.

## Troubleshooting quick table

| Symptom | Cause |
|---|---|
| Mode missing from dropdown | entry dropped by validation (Phase 5 step 2), or role's explicit `role_agent_modes` list excludes it |
| First message errors "unknown agent" | `agent_name` mismatch between `agent_modes.json` and `langgraph.local.json`, or wrong `./user_agents/...` path |
| Python edit "doesn't take" | no hot-reload — restart llamabot |
| `rails_api_request` reports a setup issue | state class doesn't extend `LlamaPressAPIState`, so `api_token` was dropped from state |
| Broken/garbled ToolMessage | state schema mismatch with what the Rails AgentStateBuilder sends — use the template's state shape |
| Overlay edits vanish after instance update | they were written inside the container instead of the mounted host files |
| Browser tools missing from the agent | `enable_live_browser_tools` site setting is off, or it was flipped without restarting llamabot |
| Page context always empty | `debug_info` not declared on the state schema, or the Rails page lacks the page-context partial |
