# Bring-your-own ChatGPT subscription (Codex OAuth) for Leo instances

**Status:** proposed — not started
**Author:** drafted 2026-08-08
**Goal:** let an instance user sign into their own ChatGPT account and have Leo run
on their Plus/Pro subscription, instead of Leo's default DeepSeek or an operator key.

---

## 1. Why, and why it's defensible

The problem this solves is model quality. Leo's default is `deepseek-v4-flash` for
cost reasons; users who already pay OpenAI $20–200/month want that capability in Leo
without us eating API spend.

**What OpenAI actually permits (researched 2026-08-08):**

- OpenAI deliberately blessed third-party coding tools using ChatGPT subscriptions.
  After Anthropic blocked OpenCode/Cline/Roo/OpenClaw from Claude Pro/Max OAuth in
  Jan 2026, OpenCode's maintainer posted that they were "working with openai to allow
  codex users to benefit from their subscription directly within OpenCode." It shipped
  in v1.1.11 and is now **built into OpenCode** (`/connect` → OpenAI → ChatGPT Plus/Pro),
  not a plugin. OpenCode's docs contrast the two vendors explicitly: Anthropic
  "explicitly prohibits this," while ChatGPT Plus is listed under services you can use
  with zero setup.
- OpenAI's own [Codex for Open Source](https://developers.openai.com/community/codex-for-oss)
  page name-checks OpenCode, Cline and OpenClaw as tools developers use.
- **There is no written ToS clause granting it.** Asked directly in
  [openai/codex#8338](https://github.com/openai/codex/discussions/8338), an OpenAI
  maintainer confirmed the Apache license permits forking, then pointed at the generic
  Terms of Use and declined to answer the subscription question. This is a tolerated
  arrangement, not a documented right.
- **"Sign in with ChatGPT" (launched Aug 2, 2026) is NOT this.** It is an identity
  product — partner apps receive name, email, profile picture. The feature request to
  add a "user plan" option so third-party apps could run inference against the user's
  own ChatGPT limits was **closed as not planned**. Do not confuse the two.

**Why our architecture clears the bar.** The concern with third-party subscription
use is many users' credentials pooled behind one operator. Each Leo instance is its
own standalone LXC container — no pooling. Stronger: OpenAI ships **device-code auth
specifically for "environments without browser access" (SSH, Docker, remote servers)**.
They built the flow for exactly this shape. A per-user container is a remote dev box.

**Design constraints that keep it that way** (each is enforced by a rule below):

| # | Constraint | Enforced in |
|---|---|---|
| 1 | Token never leaves the user's container; mothership never sees it | Phase 2 |
| 2 | Device-code auth, not scraped sessions or operator-injected tokens | Phase 2 |
| 3 | Per-container egress IP where possible | Phase 5 (infra) |
| 4 | Opt-in, never default, never a pricing lever | Phase 4 |
| 5 | Fail open to the configured default model; assume revocation | Phase 3 |

---

## 2. Correction to an earlier assumption

An earlier version of this analysis claimed the compiled-graph singleton cache
(`get_app_from_workflow_string`) would force a large refactor to thread per-user
credentials through. **That is wrong.** `get_llm(model_name)` is called *fresh on
every model call*:

- `DynamicModelMiddleware.wrap_model_call` / `awrap_model_call`
  (`app/agents/leonardo/rails_agent/middleware.py:417,440`)
- raw-node agents: `rails_beginner_agent/nodes.py:117`,
  `rails_ai_builder_agent/nodes.py:83`, `rails_plain_chat_mode/nodes.py:57`
- sub-agents: `rails_agent/sub_agents.py:230,376`,
  `rails_ticket_mode_agent/sub_agents.py:178`,
  `rails_user_feedback_agent/sub_agents.py:151`

The graph is cached; the model client is not. Credentials read inside `get_llm` are
picked up on the next turn with no restart and no graph rebuild. This is a much
smaller change than originally scoped.

---

## 3. Architecture

```
Browser (user's own machine)
  │  1. POST /api/chatgpt-auth/start
  ▼
LlamaBot container ──► OpenAI device-code endpoint
  │                      returns user_code + verification_uri
  │  2. UI shows code; user opens verification_uri on THEIR machine, approves
  │  3. container polls token endpoint until approved
  ▼
ChatGptCredential row (auth DB, this container only)
  access_token / refresh_token / account_id / plan / expires_at
  │
  ▼
get_llm("chatgpt-subscription")  ──► ChatOpenAI(base_url=<codex backend>,
                                                api_key=<fresh access token>,
                                                use_responses_api=True)
```

Device code (not the localhost-redirect PKCE flow) is the right choice: the container
has no browser, and it keeps the token acquisition on the user's side of the fence.

---

## 4. Phases

### Phase 0 — Spikes — **RESOLVED 2026-08-08**

Both answered by reading OpenAI's own Apache-licensed source in `openai/codex`
(`codex-rs/login/src/`). No live probing needed to unblock the build.

**S1 — custom `originator`: YES, there is a supported override.**
`codex-rs/login/src/auth/default_client.rs:40-41`:

```rust
pub const DEFAULT_ORIGINATOR: &str = "codex_cli_rs";
pub const CODEX_INTERNAL_ORIGINATOR_OVERRIDE_ENV_VAR: &str = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE";
```

There is also a public `set_default_originator()`. The client_id is likewise
overridable — `CLIENT_ID_OVERRIDE_ENV_VAR = "CODEX_APP_SERVER_LOGIN_CLIENT_ID"`
(`auth/manager.rs:196`). So we send `originator: llamabot` and are not impersonating
the CLI. Caveat: the env var is named `_INTERNAL_`, and only a live call proves the
*backend* accepts an unknown originator — verify on first real request, and if it
rejects us, that is the go/no-go decision, not a silent fallback to `codex_cli_rs`.

**S2 — models: Luna and Sol ARE available on a ChatGPT plan.** GPT-5.6 reached Codex
GA on 2026-07-09 with `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` selectable by
plan tier; Plus reportedly reaches all three
([openai/codex#31873](https://github.com/openai/codex/issues/31873)).

> **Registry consequence:** `gpt-5.6-luna` already exists in `get_llm` as a
> **pay-per-token API** model on `OPENAI_API_KEY`. The same model id is reachable two
> ways with two different payers. They MUST be separate dropdown entries
> (e.g. `gpt-5.6-luna` vs `gpt-5.6-luna-chatgpt`) or nobody can tell which credential
> is being billed.

**Protocol (from `login/src/device_code_auth.rs`, `login/src/server.rs`):**

- Issuer `https://auth.openai.com`; `CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"`
  (`auth/manager.rs:1618`)
- `POST {issuer}/deviceauth/usercode` `{client_id}` → `{device_auth_id, user_code,
  verification_url, interval}`
- Poll `POST {issuer}/deviceauth/token` `{device_auth_id, user_code}`; 403/404 means
  "not yet, keep waiting", 15-minute cap → `{authorization_code, code_challenge,
  code_verifier}`
- Exchange at `POST {issuer}/oauth/token` with
  `grant_type=authorization_code&code=…&client_id=…&code_verifier=…`
- Refresh URL is overridable via `CODEX_REFRESH_TOKEN_URL_OVERRIDE`

Note the device flow hands back an *authorization code*, which is then exchanged via
the normal PKCE token endpoint — it is not a direct token grant.

### Phase 0c — **RESOLVED: auth.openai.com challenges our *client*, not our IP**

Verified live from llamapress-dev (egress `167.233.45.145`) on 2026-08-08:

| Host | Result |
|---|---|
| `auth.openai.com` (sign-in, refresh) | **403** Cloudflare "Just a moment…" challenge |
| `chatgpt.com/backend-api/codex/responses` (inference) | 401 — reachable |
| `api.openai.com/v1/models` (control) | 401 — reachable |

**First diagnosis was wrong, and the correction is the whole reason this phase
resolved.** The 403 was read as an egress-IP block because it reproduced from the
container *and* the host, across four User-Agents (`llamabot`, a codex_cli_rs-shaped
one, a browser one, none at all) and two TLS stacks (httpx and curl/OpenSSL). That
evidence is equally consistent with client fingerprinting, and fingerprinting is
what it is:

> From the same host, same IP, within the same minute — `codex login --device-auth`
> issued a device code fine while httpx and curl to that host both got the 403
> challenge page.

An IP block cannot produce that split. Cloudflare is fingerprinting the client
(TLS/HTTP2 shape), which no header change reaches — hence the four fruitless UA
attempts.

**The fix is to stop being the client.** We vendor OpenAI's own Codex CLI into the
image (`Dockerfile`, pinned `CODEX_CLI_VERSION`) and delegate both sign-in and
refresh to it — `app/services/codex_cli_auth.py`. Per-user isolation comes from
`CODEX_HOME`, one directory per user, so no user's login can read or overwrite
another's. This is *more* defensible than the workarounds below, not less: the
auth step runs inside OpenAI's own signed client doing exactly what it was built
for, with nothing spoofed and no control bypassed.

We deliberately do **not** try to defeat the Cloudflare challenge by imitating a
browser's TLS fingerprint. It is fragile, and it is genuinely circumventing a
control.

Retained as fallbacks, for images that predate the vendored CLI:

1. **`CHATGPT_AUTH_PROXY`** — route only auth/refresh through a proxy on an IP
   OpenAI accepts. Inference still goes direct to chatgpt.com.
2. **`POST /api/chatgpt-auth/import`** — the user runs `codex login` on their own
   machine and pastes `~/.codex/auth.json`.

**Consequence for refresh:** refresh also hits `auth.openai.com`, so a *pasted*
credential cannot renew itself here — it dies at first expiry and `get_llm` fails
open to DeepSeek, silently. A CLI-backed login refreshes itself and does not have
this problem. Because the two are indistinguishable once stored, `status_for_user`
derives `auth_method` / `can_auto_refresh` from whether the CLI holds a login for
that user, and the UI says so plainly (chat status line, connected panel,
`/settings` card) instead of letting the user find out when the model changes
underneath them.

### Phase 0d — Codex backend quirks (learned in implementation)

The ChatGPT Codex backend is Responses-API-shaped but not identical, and each of
these surfaced as a 400 mid-turn. All are handled in `ChatOpenAICodexBackend`
(`llm_factory.py`):

| Quirk | Symptom | Handling |
|---|---|---|
| No system messages in `input` | `400 System messages are not allowed` | Hoist every `role: "system"` entry into top-level `instructions` |
| Storage must be off | `400 Store must be set to false` | `payload["store"] = False`, set **before** the no-system-message early return |
| Anthropic-style block lists | system content arrives as a list, not a string | `_flatten_text()` before hoisting |
| `originator` | — | `originator: llamabot` is **accepted**. We do not present as `codex_cli_rs`; the one part of this with no clean defense turned out not to be needed. |

Verified end-to-end 2026-08-08: 11/11 requests to
`chatgpt.com/backend-api/codex/responses` returned 200 on `gpt-5.6-sol-chatgpt`,
including tool calls and sub-agent delegation.

### Phase 0b — Remaining live checks (not blocking design)

Two unknowns that change the design. Both are cheap and should be answered first.

- **S1: Does a custom `originator` header work?** Third-party clients send
  `originator: codex_cli_rs` to the Codex backend. Presenting Leo as OpenAI's own CLI
  is the one part of this with no clean defense. Test whether the endpoint accepts a
  custom originator (e.g. `llamabot`). **If it does, use ours.** If it hard-requires
  the Codex value, that's a real go/no-go decision for Kody, not an implementation
  detail — surface it, don't paper over it.
- **S2: Which models does the subscription endpoint actually serve, and in what
  shape?** OpenHands documents `gpt-5.2-codex` (default), `gpt-5.2`,
  `gpt-5.1-codex-max`, `gpt-5.1-codex-mini`. Confirm the live list, confirm streaming
  works, and confirm tool-calling round-trips through LangChain's Responses-API path.

Deliverable: a throwaway script under `scratch/` (not committed) plus findings written
into this doc.

> **Gotcha while spiking on this box:** `MODEL_SWITCHING_ALLOWED` is unset here and
> defaults to `False` (`model_policy.py:72`), so `get_llm` will silently hand back
> DeepSeek for any new model name and the spike will look like it "works" while
> testing nothing. Set the env var before probing, or add the name to
> `ENABLED_MODELS`.

### Phase 1 — BYO API key (ships first, standalone value)

Not a detour — it builds the exact credential plumbing OAuth needs, with zero policy
risk, and serves users who have API credits.

- New model entry `openai-byo-key` in `get_llm`, reading a per-instance key from the
  credential store rather than `OPENAI_API_KEY`.

> **This entry is exactly the shape of the bug `provider_key()` was written to stop.**
> Both new models point a `ChatOpenAI` at a non-OpenAI `base_url`, and the OpenAI SDK
> falls back to the ambient `OPENAI_API_KEY` whenever `api_key` resolves to `None` —
> which would send the *operator's* key to the Codex backend the moment a user's
> credential is missing or expired. Resolve the key through
> `llm_factory.provider_key()` (`llm_factory.py:122`), never a bare `os.getenv` or a
> nullable DB read, and extend `test_provider_key_never_leaks_openai.py` to cover both
> new model names.
- Register in `model_policy._KNOWN_MODELS` and `model_capabilities.MODEL_CAPABILITIES`
  (`{'images': True, 'video': False, 'pdf': False}`). **Both** — an unregistered model
  hits the permissive-default trap documented at `model_capabilities.py:57`.
- Settings UI to paste/clear a key. Never echo it back — return a masked
  `sk-…abcd` presence indicator only.

### Phase 2 — Credential storage

**Do not reuse `SiteSetting` for this.** `SiteSetting.value` is
`max_length=1000` (`app/models.py:213`); OpenAI OAuth access tokens are JWTs that
routinely exceed that, and an access + refresh pair certainly does. This will fail in
production, not in tests, if we skip it.

New table `chatgpt_credential`:

| column | notes |
|---|---|
| `user_id` | FK to `user.id`. Instances can have multiple users (`User` has `role`, `llamapress_user_guid`) — scope per user, not per instance. |
| `access_token`, `refresh_token` | encrypted at rest; `Text`, not `String(1000)` |
| `account_id` | the ChatGPT account id header value |
| `plan_tier` | for UI display ("connected as Pro") |
| `expires_at` | drives proactive refresh |
| `created_at`, `last_refreshed_at` | |

Encryption key derivation follows the `token_service.SESSION_SECRET` precedent
(`app/services/token_service.py:26-62`): env override → auth-DB row → ephemeral
fallback. That pattern already solved "must survive a container recreate," which is
the same requirement here — see the incident note in that docstring.

**Constraint 1 is a code rule, not a comment:** no route, no telemetry payload, no
error report, and no mothership call may include these columns. Add them to whatever
redaction list the feedback/error snapshot builder uses.

### Phase 3 — OAuth device flow + refresh

- `app/routers/chatgpt_auth.py`: `POST /start`, `POST /poll`, `DELETE /disconnect`,
  `GET /status`. Engineer-role gated.
- Refresh on demand inside `get_llm` (check `expires_at`, refresh if within N minutes),
  not on a background timer — containers idle and a timer would drift.
- New model entry `chatgpt-subscription` in `get_llm`, plus the same
  `_KNOWN_MODELS` / `MODEL_CAPABILITIES` registration as Phase 1.
- **Fail-open (constraint 5):** any auth failure — revoked token, refresh failure,
  endpoint 404/403 — logs a warning, marks the credential disconnected, and returns
  `enabled_default_model()`. A dead OpenAI arrangement must degrade to DeepSeek, never
  to a broken chat turn. This is the kill switch: it triggers itself.

### Phase 4 — UI and disclosures

Connect/disconnect in settings, showing connected account + plan tier. Model dropdown
shows the subscription entry only when connected.

Two disclosures at the connect step, not buried:

- **Data:** usage is governed by consumer ChatGPT terms, which include training on
  conversations unless the user has turned that off in their own ChatGPT data controls.
  This breaks the ZDR posture we built the Fireworks routing for
  (`llm_factory.py:207-239`) — users routing client code through a personal Plus
  account need to know that.
- **Risk:** their account carries it. Suspensions with no explanation and slow appeals
  are documented. Say so plainly.

Constraint 4: this is never the default model, and no Leo tier is priced on the
assumption that users bring a subscription.

### Phase 5 — Infra (parallel, not blocking)

Per-container egress IP. **This is the most likely thing to actually bite us** —
N ChatGPT accounts NAT'd behind one Hetzner IP reads as account sharing to abuse
detection regardless of intent. If per-container IPs aren't feasible, the fallback is
a documented cap on how many subscription-connected instances share an egress address.

---

## 5. Tests (failing-test-first, per `~/dev/CLAUDE.md`)

Assert structure, never LLM text.

- `test_chatgpt_credential_not_in_site_settings` — the token is unreachable via
  `/api/site-settings` (mirrors how `session_secret` is kept out of
  `VALID_SITE_SETTINGS`).
- `test_chatgpt_token_never_leaves_container` — error-report and feedback-snapshot
  payload builders redact the credential columns.
- `test_expired_token_refreshes_before_call` — `get_llm` refreshes when `expires_at`
  is in the past.
- `test_revoked_token_falls_back_to_default_model` — a 401 from the endpoint yields
  `enabled_default_model()`, not an exception. **The most important test in the set.**
- `test_missing_credential_does_not_send_the_openai_key` — extend
  `test_provider_key_never_leaks_openai.py` with both new model names, asserting the
  operator's `OPENAI_API_KEY` never reaches the Codex `base_url` when the user's
  credential is absent.
- `test_subscription_model_registered_everywhere` — extend the existing
  `test_model_registry_consistency.py` so the new entries can't be half-registered.
- `test_disabled_by_policy` — `DISABLED_MODELS` still gates it; `get_llm` remains the
  authoritative chokepoint (`model_policy.py:37`).
- `test_credential_scoped_per_user` — user A's token is not reachable by user B on a
  multi-user instance.

---

## 6. Open questions for Kody

1. **S1 outcome** — if the Codex backend requires the `codex_cli_rs` originator, do we
   ship anyway? (My read: this is the one genuinely uncomfortable piece.)
2. **Per-container egress IPs** — feasible on the current Hetzner/LXC setup, or do we
   accept shared-IP risk?
3. **Multi-user instances** — confirm whether any real instance has >1 user. If they're
   all effectively single-user, Phase 2 simplifies, but the FK costs nothing now and
   retrofitting it later is painful.

---

## 7. Sources

- [Codex auth docs](https://learn.chatgpt.com/docs/auth) — device code for headless envs
- [openai/codex discussion #8338](https://github.com/openai/codex/discussions/8338) — ToS non-answer
- [OpenCode providers](https://opencode.ai/docs/providers/) — built-in, Anthropic contrast
- [Codex for Open Source](https://developers.openai.com/community/codex-for-oss)
- [OpenHands LLM subscriptions](https://docs.openhands.dev/sdk/guides/llm-subscriptions) — model list, PKCE
- [OpenAI service terms](https://openai.com/policies/service-terms/)
