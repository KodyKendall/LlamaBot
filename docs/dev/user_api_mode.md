# llamapress_api — Rails HTTP scoped to the signed-in user

`app/lib/llamapress_api.py` is a **library** exporting one tool, `rails_api_request`,
which calls the Rails app's JSON API **as the signed-in user**, bounded by that user's
own Rails permissions — plus `LlamaPressAPIState`, the state base that carries the token.

It exists because every other Rails-touching tool in this repo (`bash_command`,
`rails_api_sh`) shells into the Rails container and gets unrestricted ActiveRecord.
Those bypass Pundit entirely and are only safe because just the `engineer` role is
granted them. This tool is the one you can hand to an agent used by people you
*don't* trust with the box.

**LlamaBot deliberately ships no agent and no chat mode around it.** The working
example lives in the CLIENT repo: Leonardo's `langgraph/agents/leo/nodes.py`,
registered via the `langgraph.local.json` overlay + `agent_modes.json`. That
end-to-end path — custom agent downstream, library from the platform — is the
product story, and `test_llamapress_api.py` pins the boundary so a built-in mode
doesn't quietly re-grow here.

## How the scoping actually works

Nothing in Python authorizes anything. The guarantee is inherited from Rails:

1. **Rails mints** a signed token per chat turn — `message_verifier(:llamabot_ws)`,
   payload `{session_id, user_id}`, 30-minute TTL
   (`llama_bot_rails/app/channels/llama_bot_rails/chat_channel.rb`).
2. **LlamaBot cannot forge one.** It has no `secret_key_base`. This is the whole
   ballgame: a compromised agent cannot sign `user_id: <an admin>`. Verified —
   a forged token returns `401 LLAMA_AUTH_004 "Token signature verification failed"`.
3. **The gem verifies + signs in** via warden, so `current_user` in the controller is
   genuinely that person (`lib/llama_bot_rails/agent_auth.rb`).
4. **`llama_bot_allow :index, :show`** allowlists which actions a token-authed request
   may reach at all — 403 before any policy runs.
5. **Pundit** then narrows per-user as always.

Widening reach is a **Rails-side act** (add `llama_bot_allow`), never a Python-side
one. Don't add an escape hatch to the tool.

## ⚠️ The gotcha: `llama_bot_allow` only gates controllers that opt in

This is the non-obvious part, and it bit during development.

`llama_bot_allow` constrains a controller **only if that controller includes
`LlamaBotRails::AgentAuth`**. A controller that never opted in is gated by the app's
*own* authentication — and the Leonardo skeleton ships with it **commented out**:

```ruby
# rails/app/controllers/application_controller.rb
# AUTHENTICATION IS DISABLED BY DEFAULT FOR NEW PROJECTS.
# before_action :authenticate_user_from_token!
# before_action :authenticate_user!
```

Measured on the dev box:

| Request | Result |
|---|---|
| `GET /api/users` **with no token** | `401` — AgentAuth working |
| `GET /users` **with no token** | `500` — routed into the controller, **ungated** |

So without a path restriction, the tool would reach every un-gated route and
the per-account scoping would be a fiction. Hence `ALLOWED_PATH_PREFIX = "/api/"` in
`app/lib/llamapress_api.py`.

**That prefix is a convention, not a proof.** A controller added under `/api/` that
forgets `include LlamaBotRails::AgentAuth` is ungated too. The real fix is for the
app to enable authentication — then `authenticate_user!` (aliased by the gem to
`authenticate_user_or_agent!`) demands a user *and* enforces the allowlist on every
controller. **If you enable `user_api` on an instance, enable the app's auth too.**

## Wiring — how a custom agent uses it

Two imports, in the client repo (e.g. Leonardo `langgraph/agents/<name>/nodes.py`,
mounted into the container at `/app/app/user_agents`):

```python
from app.lib.llamapress_api import LlamaPressAPIState, rails_api_request

class MyState(LlamaPressAPIState):   # puts api_token on the schema — mandatory
    ...

tools = [rails_api_request]
```

Then register the graph in `langgraph.local.json` and the chat-dropdown entry in
`agent_modes.json`. Leonardo's `leo` agent is the working reference for all of this.

The token rides the WS frame as `api_token` and lands in state automatically —
`request_handler.get_langgraph_app_and_state` passes every non-routing field through
(`app/websocket/request_handler.py`). But **only if declared**: LangGraph filters
state to the schema, so forgetting the `LlamaPressAPIState` subclass silently
disables the tool (it fails closed with a "setup issue" message). The built-in
agents' `RailsAgentState` deliberately does NOT declare it — they don't use the
tool, and declaring it would persist a bearer credential into their checkpoints.

Where the token comes from: the chat frontend fetches it from the gem's
`GET /llama_bot/agent/token` (Devise session required, CORS-allowlisted for the
chat UI's origin) and attaches it to every message. Rails-embedded chat
(`chat_channel`) mints and passes it directly.

**Never give LlamaBot `secret_key_base` to shortcut any of this.** That collapses
step 2 and the entire guarantee with it.

## Don't

- Don't register a built-in LlamaBot agent or chat mode around this tool — the
  agent belongs in the client repo. `test_llamapress_api.py` pins this.
- Don't put it in an agent alongside tools the Rails token doesn't bound.
  `bash_command` would hand that agent's users the Rails console.
- Don't log or prompt-inject the token. It's a bearer credential for that user.
