# The builder over MCP (`/mcp`) — LP-50, 0.7.20.2

Every box serves its builder (the LlamaBot agent that edits the app) as an MCP
server, so the mothership (Leo at llamapress.ai/ai, the phone app) and outside AIs
can drive it without a WebSocket or a browser session.

Code: `app/mcp_server.py` (endpoint + tools), `app/services/mcp_auth.py` (tokens,
switch, rate limit). Tests: `app/tests/test_mcp_builder_server.py`.

## Endpoint

`POST https://<dns>.leo.llamapress.ai/mcp` — LlamaBot's host, not `rails-`.

- MCP Streamable HTTP, **stateless**, JSON responses (no `Mcp-Session-Id`). Send
  `Accept: application/json, text/event-stream` and `Content-Type: application/json`.
- `initialize`, `notifications/initialized` and `ping` answer without a token.
  Everything else needs one.
- `GET /mcp` → 405 (no server-push stream; poll `get_run`).
- No token / bad token → `401` with
  `WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource"`.
- Over the rate limit (default 120 requests/minute per token,
  `LLAMABOT_MCP_RATE_LIMIT_PER_MINUTE`) → `429`, `Retry-After: 60`.
- Switched off → `404` for `/mcp` and the metadata document.

**Bearer only, no OAuth.** `/.well-known/oauth-protected-resource` (and the
`/.well-known/oauth-protected-resource/mcp` form) exists only so the 401 can point
somewhere; it lists `bearer_methods_supported: ["header"]` and no
`authorization_servers`. Claude.ai / ChatGPT connectors that insist on OAuth
cannot connect yet; Claude Code and the mothership can (custom header).

## Auth: `Authorization: Bearer <token>`

One of three tokens. Every one maps to a principal with the same role / mode grant
/ thread ownership rules as `/ws`. `WS_AUTH_REQUIRED=false` does **not** relax `/mcp`.

### 1. Mothership token (what the mothership should send)

Same format, same per-box secret and same verification code as the magic-link
login (`GET /login?token=`, `app/services/magic_link_service.py`):
the secret is `LLAMAPRESS_AI_LOGIN_SECRET`, i.e. the box's login secret
(`Instances::LoginSecret` / what `LlamapressLoginToken` signs with). One extra
claim, `aud`, which must be exactly `"llamabot_mcp"`:

```
payload_b64 = base64url_nopad(JSON {"username": "<box username>", "exp": <unix seconds>, "aud": "llamabot_mcp"})
sig_b64     = base64url_nopad(HMAC-SHA256(key = LLAMAPRESS_AI_LOGIN_SECRET, data = payload_b64))
token       = payload_b64 + "." + sig_b64
```

Ruby (mothership):

```ruby
payload = { username: username, exp: 5.minutes.from_now.to_i, aud: "llamabot_mcp" }
p64 = Base64.urlsafe_encode64(payload.to_json, padding: false)
sig = Base64.urlsafe_encode64(OpenSSL::HMAC.digest("SHA256", login_secret, p64), padding: false)
token = "#{p64}.#{sig}"
```

Rules:

- `username` = the same box username the magic-link login uses. It must be an
  active LlamaBot user on that box; the run executes **as that user** (their role,
  modes, threads; the thread is saved as theirs and shows in their chat list).
- `exp` must be in the future (30 s skew) and **at most 1 hour ahead**. Mint per use.
- A login link (no `aud`) is refused by `/mcp`; an MCP token is refused by
  `/login?token=`. A token signed with another box's secret fails the signature.
- Note: this is the HMAC login secret. `/auth/consume` grants are opaque and
  redeemed server-to-server (`MothershipClient.verify_login_grant`) — they are not
  signed with a box secret, so there is nothing of theirs to reuse for a bearer token.

### 2. The box's ws JWT

`GET /api/ws-token` with a signed-in session (what the browser uses for `/ws`).
Three dot-separated parts; verified by `verify_ws_token`.

### 3. llama_bot_rails gem token

`<base64>--<hex>` MessageVerifier token, verified by `verify_rails_token`
(role `rails`, owns no threads — same as on `/ws`).

## The switch

On when ALL of these allow it (checked on every request, so a push lands at once):

1. The mothership's model policy (already pushed on lease renewal and the health
   report; stored in `.leonardo/model_policy.json`) does not say `"mcp_enabled": false`
   — top level (fleet) or inside `instance_overrides` (this box).
2. `LLAMABOT_MCP_ENABLED` is not `0/false/no/off`.
3. If `LLAMABOT_MCP_ENABLED` is unset: the box is not ZDR (ZDR/FERPA boxes are off
   by default; turning one on needs `LLAMABOT_MCP_ENABLED=true` **and** Privacy Lead approval).

The policy can only switch it off, never force it on.

## Tools

Results are JSON text; each stays under ~12k characters. Errors come back as an MCP
tool error (`isError: true`) with a one-line reason.

| Tool | readOnly | Arguments | Returns |
| --- | --- | --- | --- |
| `send_message` | no | `message` (str, required), `thread_id?` (omit = new chat), `agent_mode` (default `"engineer"`), `llm_model?`, `ask_before_edits` (bool, default false), `wait_seconds` (0–60, default 0) | `{thread_id, run_id, status, text_so_far}`; `status` is `queued` when the thread was mid-run |
| `get_run` | yes | `run_id`, `after_event?` (int cursor), `wait_seconds` (0–25, default 25 — long-poll: without `after_event`, until the run stops running; with it, until there are newer steps) | `{run_id, thread_id, status, text, steps: [{type, title, detail}], events_cursor, more, pending?}` |
| `answer` | no | `run_id`, `approval?` (`"approve"`/`"reject"`), `answer?` (text; the reason on a reject) | `{run_id, thread_id, status}` — the continued run's id |
| `cancel_run` | no | `run_id` | `{run_id, cancelled}` |
| `list_threads` | yes | `limit` (1–50, default 20) | `{threads: [{thread_id, title, updated_at, message_count, agent_name}]}` |
| `get_thread` | yes | `thread_id`, `limit` (1–100, default 20) | `{thread_id, messages: [{role, text}]}` |
| `list_agent_modes` | yes | — | `{modes: [{key, agent_name}]}` — pass `key` as `agent_mode` |

- `status`: `running | waiting_for_approval | waiting_for_answer | done | error | cancelled`.
- `run_id` is `<thread_id>@<seq>`: the thread plus the position in the thread's
  output log where the run began. Treat it as opaque.
- `steps[].type`: `tool_call`, `tool_result`, `file_edit`, `todo_update`, `message`,
  `approval_request`, `question`, `error`, `notice`, `background_task`.
- Paging: pass `events_cursor` back as `after_event`. `more: true` = call again now.
- `pending` (when waiting) is the approval request (`action_requests`) or question
  frame exactly as the browser receives it.

Typical loop: `send_message` → `get_run` (long-polls) until not `running` → if
`waiting_*`, `answer` and keep polling the returned `run_id`.

## How it reuses the chat

`send_message` builds the same frame the browser sends on `/ws` and submits it to the
same app-wide `RunManager` (`RequestHandler._queued_run_factory` → `handle_request`).
So the paywall, instance lock, ZDR, tool limits, HITL approvals and auto-save apply
unchanged; a browser tab open on that thread sees the run live; the run survives the
MCP caller going away. `/ws` frames and behaviour are unchanged.

Not durable across a LlamaBot restart (same as `/ws` replay): `get_run` on a run from
before a restart says "Unknown run" — read the result with `get_thread`.

## Logging

One INFO line per tool call: `MCP tool=… principal=… thread=… run=… outcome=… ms=…`.
Never the message text.
