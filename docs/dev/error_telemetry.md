# Error Resilience & Telemetry — design spec

**Status:** Design / proposal
**Author:** RCA prompted by two production errors (2026-07): a `cache_control` crash and a `gpt-5-nano` image `400`.
**Scope:** LlamaBot side (this repo) is in-scope. The LlamaPress.ai mothership dashboard/alerting is specified as a contract only.

---

## 0. TL;DR

Two problems, one root cause, one design.

1. **Users hit raw provider errors** (`Error processing request: <traceback>`) and get a degraded, dead-end experience.
2. **We never find out** — the error logs to one instance's stdout and dies there.

The root cause of the *errors themselves* is a single disease: **request construction that is blind to the target model's exact API contract** — sometimes on the *kwarg* axis (`cache_control`), sometimes on the *content-format* axis (`image_url`). See §1.

The design has two halves:

- **A resilience ladder** (§3) so no user ever sees a raw provider error again — the *net*. Most rungs already exist in the codebase; we consolidate and add two.
- **`report_error` telemetry to the mothership** (§4) so the LlamaPress team sees failures immediately and knows which ones to cure next — the *prioritizer*.

Guiding principle: **the net catches the infinite set of mismatches we haven't discovered; telemetry tells us which ones actually hurt people; we cure those.** We do not try to enumerate every model×feature combo up front — that's the over-engineering trap.

---

## 1. Root cause — the same disease, two axes

### 1a. `cache_control` (kwarg axis)

`Completions.create() got an unexpected keyword argument 'cache_control'`

`rails_ai_builder_agent` called `llm.invoke(messages, cache_control={"type": "ephemeral"})`. `cache_control` is a runtime kwarg; LangChain's OpenAI-family clients merge extra `.invoke()` kwargs straight into the request payload and call `client.chat.completions.create(**payload)`. Anthropic accepts `cache_control`; DeepSeek's OpenAI-compatible endpoint rejects unknown kwargs → `TypeError`. The call was **unconditional** — provider-agnostic. Fixed on `0.4.1-alpha` by commit `e9c45da` (0.3.5n) with a `llm_model.startswith("claude")` guard. Any instance on a **pre-`v0.4.0`** build is still exposed.

### 1b. `gpt-5-nano` image (content-format axis)

`400 ... unknown variant 'image_url', expected 'text'`

`gpt-5-nano` is wired with `use_responses_api=True` (`llm_factory.py:171-177`) — OpenAI's **Responses API**, whose image content block is `{"type": "input_image", "image_url": ...}`. Our message-building code emits the **Chat Completions** shape `{"type": "image_url", "image_url": {"url": ...}}`. The Responses API deserializer only knows `text`/`input_image` → `400 unknown variant 'image_url'`.

Crucially, the capability table (`model_capabilities.py:29`) is *correct* that `gpt-5-nano` supports images — the bug is the **wire format**, which the boolean table doesn't model.

### 1c. Why they're the same

Both are request construction that doesn't match the selected model's exact API contract:

| Bug | Axis | What we sent | What the target wanted |
|---|---|---|---|
| `cache_control` | kwarg | `cache_control=` on a non-Anthropic client | no such kwarg |
| `image_url` | content format | Chat-Completions image block | Responses-API image block |

You cannot enumerate every such mismatch in advance (new models, new APIs, new features arrive constantly). So the architecture must **fail gracefully on the unknowns** and **cure the known ones**, in that order.

### 5 Whys (why a user was affected AND why we didn't know)

1. User saw a raw error → a provider/API rejected our request.
2. Why rejected → request built without regard to that model's exact contract.
3. Why unguarded → model *selection* was centralized (`get_llm`), but request *shaping* (kwargs, content format) was left scattered at call sites.
4. Why no test caught it → no fake-LLM/e2e exercising these agents against the *specific* non-default model + content combo.
5. **Why we didn't find out → no error telemetry.** Logs to one instance's stdout, string to the user's screen, end of trail. (The real RCA — §2 & §4.)

---

## 2. What happens to an error today (traced)

```
Request throws
      │
      ▼
request_handler.py:824  except Exception as e:
      ├─ line 825:  logger.error(..., exc_info=True)   →  stdout of ONE container (no shipping)
      └─ line 830:  websocket.send_json({"type":"error",
                      "content": f"Error processing request: {e}"})   →  user's screen
                                                                          │
                                                                          ▼
                      MessageHandler.js:1300  addMessage(content,'error') → rendered, then GONE
```

Same pattern at `request_handler.py:952` (`handle_approval_response`) and `:1160` (`handle_question_response`).

### Inventory — what already exists vs. what's missing

| Capability | Status | Location |
|---|---|---|
| Server-side stack trace | ✅ but stdout-only | `request_handler.py:825` (`exc_info=True`) |
| Off-box aggregation (Sentry/Datadog) | ❌ none | — |
| Frontend error reporting | ❌ rendered & discarded | `MessageHandler.js:1300` |
| Phone-home channel to mothership | ✅ **exists, in prod** | `app/services/mothership_client.py` |
| `report_error` method on it | ❌ **missing — the gap** | — |
| **Model retry** | ⚠️ exists, **Google-only** | `rails_agent/middleware.py:395` (`with_retry`, `ResourceExhausted` only) |
| **Tool-call auto-repair** | ✅ **exists, all agents** | `RepairOrphanedToolCallsMiddleware`, prepended by `build_leonardo_agent` (`agent_factory.py:112`) |
| **Model fallback** | ❌ missing | — |
| **Capability preflight (multimodal)** | ⚠️ exists but coarse | `model_capabilities.py`, consulted at `request_handler.py:1392` & `middleware.py:279` |
| **Graceful "Leo still talks" floor** | ❌ missing | — |

The takeaway: this is **consolidation, not a new subsystem.** The chokepoint every LLM call already flows through — `wrap_model_call` in the middleware — is where the ladder belongs.

---

## 3. The resilience ladder (the net)

Different error classes need different rungs. **Blindly chaining "retry 3× → switch model → switch mode" is the over-engineering trap** — retrying a *deterministic* error (both bugs above) just wastes time failing identically. Each rung is chosen by what the previous failure *tells* us.

| Rung | Trigger | Action | Status |
|---|---|---|---|
| **0. Tool-call repair** | malformed/orphaned tool call | re-feed error to model | ✅ exists |
| **1. Retry same model** | *transient only* (429, timeout, 5xx, connection) | `with_retry` + backoff, silent | ⚠️ broaden from Google-only |
| **2. Fallback model** | anything rung 1 didn't fix | `with_fallbacks([...])` → another enabled model | ➕ add (one-liner) |
| **3. Graceful floor** | the *graph itself* threw | **stripped-down** DeepSeek call → friendly message + escape hatches | ➕ add (the UX win) |
| **—. Report + escalate** | any rung fired | `report_error` to mothership + [Contact support] | ➕ §4 |

### Two hard rules (these keep it robust, not just clever)

1. **Retry the model call, never the whole turn.** Tools have side effects (Rails writes). Re-running a whole failed turn can double-write. The existing middleware already retries at the safe level (the model call) — keep it there.
2. **Escalation is *within a single turn*, and mode-switching is an *offer*, not automatic.** No cross-session "3 errors → switch mode" counter (stateful bug farm). Silently moving a user into beginner mode mid-conversation is surprising UX. When rung 3 fires, *offer* a simpler retry — the user stays in control.

### Rungs 1–2 live in `wrap_model_call` (`rails_agent/middleware.py:389`)

> **Rung 1 also lives outside the middleware.** Raw StateGraph nodes
> (`rails_beginner_agent`, `rails_ai_builder_agent`) invoke the model directly and
> never touch `wrap_model_call`, so before 2026-07-12 they had *no* transient retry
> at all — a single connection blip killed the turn. They now wrap each direct
> `.invoke(...)` in `resilience.invoke_with_transient_retry(fn)`, which shares the
> same classifier + backoff constants as the middleware loop. **Any new raw-node
> agent must do the same** (or be built via `create_agent`, which gets the
> middleware for free). Also note `httpx.ReadError`/`WriteError` (mid-stream socket
> failures, empty `str(e)`) are classified transient as of the same date.

Today (Google rate-limits only):

```python
model = get_llm(llm_model)
model = model.with_retry(
    retry_if_exception_type=(ResourceExhausted,),
    stop_after_attempt=5,
    wait_exponential_jitter=True,
)
```

Target — broaden retry to the *transient* class, then add a capability-aware fallback:

```python
model = get_llm(llm_model)
# Rung 1: retry only genuinely transient failures (never deterministic 4xx)
model = model.with_retry(
    retry_if_exception_type=(ResourceExhausted, APITimeoutError, APIConnectionError, InternalServerError),
    stop_after_attempt=3,
    wait_exponential_jitter=True,
)
# Rung 2: fall back to a second enabled model if the primary keeps failing.
#   Capability-aware: if the request carries an image, the fallback must be a
#   vision-capable model; otherwise fall back to the text floor (DeepSeek).
fallbacks = choose_fallbacks(llm_model, request)   # returns [] when none apply
if fallbacks:
    model = model.with_fallbacks([get_llm(m) for m in fallbacks])
return handler(request.override(model=model))
```

`with_fallbacks` alone would have **silently absorbed the `cache_control` crash** (fall to a model whose client accepts it, or to the stripped floor).

### Rung 3 lives at the outer net (`request_handler.py:824`)

When the graph *itself* throws (not just one model call), replace the raw traceback with a **single bare DeepSeek call — no tools, no `cache_control`, and with unsupported/unsendable content stripped** (minimal surface area, so it works even when the fancy path is broken):

```python
except Exception as e:
    logger.error(f"Error handling request: {str(e)}", exc_info=True)
    await self._report_error(e, incoming_message)          # §4, fire-and-forget
    if self._is_websocket_open(websocket):
        msg = await self._graceful_degrade(e, incoming_message)  # bare DeepSeek, content stripped
        await websocket.send_json({"type": "error_recovered", "content": msg,
                                   "actions": ["retry_simpler", "contact_support"]})
    raise e
```

Producing, e.g.:

> *"I ran into a snag working on that and I've logged it to our team. Want me to try a simpler approach, or send this to support?"* → **[Try again simpler] [Contact support]**

**Why the floor must strip content:** the `gpt-5-nano` image case proves it. If the floor call still carries the `image_url` block, it re-triggers the same `400`. Stripping unsendable content guarantees the floor *always* resolves to "Leo answers your text + an honest note about the image," never a second raw error. This is the single change that makes the ladder actually cover the multimodal case.

### How the ladder handles each worked example

| | `cache_control` | `gpt-5-nano` image |
|---|---|---|
| Rung 1 (retry) | ❌ deterministic | ❌ deterministic |
| Rung 2 (fallback) | ✅ dodges via other model | ⚠️ only if it lands on a Chat-Completions vision model |
| Rung 3 (floor, content-stripped) | ✅ Leo still answers | ✅ Leo answers text + honest note re: image |
| Net user experience | recovered | recovered |

The ladder **recovers the UX in both cases**, but it's a *net*, not a *cure* — the image still didn't work. Curing that is §5.

---

## 4. Telemetry — `report_error` to the mothership (the prioritizer)

### Layer 1 — `MothershipClient.report_error()`

Modeled exactly on `report_disconnect` (gated on `self.enabled`, fire-and-forget, never raises, short timeout). Append to `app/services/mothership_client.py`:

```python
async def report_error(
    self, *, thread_id, error_class, error_message, traceback_str,
    agent_mode=None, model=None, llamabot_version=None,
    occurred_at=None, fingerprint=None, recovered=None,
) -> None:
    """
    POST /api/leonardo/report_error
    Fire-and-forget telemetry for errors that reached the resilience net.
    Never raises — a reporting failure must never worsen the user's error.
    """
    if not self.enabled:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            payload = {
                "instance_name": self.config["instance_name"],
                "error_class": error_class,
                "error_message": error_message[:2000],
                "traceback": traceback_str[:5000],
            }
            for k, v in {
                "thread_id": thread_id, "agent_mode": agent_mode, "model": model,
                "llamabot_version": llamabot_version, "occurred_at": occurred_at,
                "fingerprint": fingerprint, "recovered": recovered,
            }.items():
                if v is not None:
                    payload[k] = v
            r = await client.post(
                f"{self.config['mothership_url']}/api/leonardo/report_error",
                json=payload,
                headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.warning(f"Error report failed (HTTP {e.response.status_code}): {e.response.text}")
    except httpx.RequestError as e:
        logger.warning(f"Error report request failed: {e}")
    except Exception as e:
        logger.warning(f"Error report unexpected error: {e}")
```

**Field rationale.** `instance_name` = *which customer*. `model` + `agent_mode` = would have instantly said "DeepSeek + rails_ai_builder" / "gpt-5-nano + image". `llamabot_version` = surfaces "instance on stale code" (the pre-`v0.4.0` `cache_control` case) without asking anyone. `fingerprint` (hash of `error_class` + normalized message + `agent_mode`) = dedupe/count instead of drowning in occurrences. `recovered` = did the ladder save it (rung 3) or was it a hard failure — lets the team distinguish "annoying but handled" from "user stuck."

### Layer 2 — call it from the three handlers

A `_report_error(self, e, incoming_message)` helper (fills `model`/`agent_mode`/`llamabot_version` from `agent_config` + the version source `main.py:288` already uses), called inside the `except` at `request_handler.py:824`, `952`, `1160`, wrapped in its own try so telemetry can never cascade.

### Layer 3 — mothership contract (LlamaPress.ai, NOT built here)

1. **Endpoint** `POST /api/leonardo/report_error` — auth by bearer token → resolve `instance_name` → `UserInstance` (same as `report_message`).
2. **Store** `InstanceError` rolled up by `fingerprint`: `count`, `first_seen`, `last_seen`, latest `model`/`agent_mode`/`llamabot_version`/`traceback`, `recovered` ratio. One row per unique error (Sentry-style), not per occurrence.
3. **Dashboard** on the production admin: grouped by fingerprint, filterable by instance / model / version. "Stale-version instances still hitting fixed bugs" and "gpt-5-nano 400ing on images across N instances" fall out for free.
4. **Alerting (SMS/email):** via the existing transactional provider (SendGrid/Postmark/Twilio).
   - **New fingerprint** (never-before-seen error) → the high-signal event; good candidate for real-time SMS.
   - **Volume spike** on a known fingerprint → an instance breaking en masse; per-fingerprint throttle so one broken instance can't text you 500×.
   - Suggested rollout: **daily email digest first; add SMS for new-error-class only.**
5. **[Contact support] button** POSTs to the mothership (reuse `report_error` with a `source: "user_support_request"` flag or a sibling endpoint), which emails **support@llamapress.ai** with the fingerprint + thread pre-attached. No email infra on the instance; support gets a filled-in incident, not "it broke."

---

## 4b. Agent-reported friction — `report_friction` (the papercut channel)

Everything above is about things that *crash*. But most of what actually slows a Leo
down never raises anything: a tool errors in a way its description never warned about,
a file is root-owned so every edit silently fails, output contradicts the docs, a
capability just isn't there and gets routed around. None of that produces an
`InstanceError` row — it dies in the transcript, and we only ever learn about it
secondhand, from a customer complaining about the *downstream* symptom.

`report_friction` (`app/agents/leonardo/friction.py`) is the agent's own channel for
it. The Leo files a short structured complaint about **its own tooling**, and it ships
down this exact pipeline — `MothershipClient.report_error` → `/api/leonardo/report_error`
→ the same `InstanceError` queue. No new plumbing, no new endpoint.

**Payload mapping:**

| report_error field | friction value |
|---|---|
| `source` | `"agent_friction"` |
| `error_class` | `AgentFriction.<category>` (e.g. `AgentFriction.permissions`) |
| `error_message` | the agent's `what_happened` |
| `traceback` | the details blob — severity, category, tool, agent_mode, model, verbatim `evidence`, `suggested_fix` |
| `recovered` | `severity != "blocked"` — i.e. did the agent get past it |
| `fingerprint` | md5 of `category\|tool_name\|first line`, same shape as the backend's |

**Where it's wired:** engineer, engineer plan, beginner, beginner plan (the generic
`rails_plan_mode_agent`, so database/ai_builder/testing/pyxl plan variants get it too),
ticket, ticket plan — plus both delegated sub-agents (`delegate_task`,
`delegate_research`), which run in isolated context and therefore see friction the main
agent never witnesses. Tool registration AND prompt wiring are pinned per mode by
`app/tests/test_report_friction.py`.

**Three properties it must keep** (they are the tests, not aspirations):

1. *It can never hurt the turn.* Bad enum values are coerced, not rejected; a broken
   runtime is swallowed; the POST runs on a detached daemon thread so the agent never
   waits on it. The tool always returns a string, and every return string — including
   the dropped ones — reads as "noted, keep going", never as an error worth retrying.
2. *It can't flood the queue.* Deduped by fingerprint and capped at
   `MAX_REPORTS_PER_THREAD` (3) per conversation. Sub-agents inherit the parent's
   config, so they share that budget rather than getting a fresh one.
3. *The prompt does the work, not the description.* A tool description alone does not
   get a tool used. `FRICTION_PROMPT_SECTION` states the triggers, plus the two rules
   without which the model either stops after reporting or narrates the report at the
   user: it is invisible to the user, and it fixes nothing right now — carry on.

It is appended **after** the project-context overlay (`with_friction_section`), not
baked into each mode's prompt constant, so a mothership-delivered prompt override —
which replaces the base prompt wholesale — can't silently strip the instructions for a
tool the agent still has.

**⚠️ Mothership TODO:** `/api/leonardo/report_error` allowlists `source` to
`%w[llamabot rails_app frontend]` and silently defaults anything else to `"llamabot"`.
Until `agent_friction` is added to that allowlist, these reports land in the queue
wearing the wrong source label — filter on `error_class LIKE 'AgentFriction.%'` in the
meantime. Do **not** merge friction into the exception stream in the dashboard: it is
self-reported and subjective, with no traceback, and mixing it in wrecks triage. It
wants its own view (or at minimum its own filter), ideally sorted by `count` — a
papercut 40 instances all report is a roadmap item.

---

## 5. Curing the known cases (proactive, upstream of the net)

The net makes failures graceful; these make the *common, known* cases actually succeed.

1. **Model-aware content format (cures 1b).** Build image blocks in the shape the selected model's API expects. Cleanest: feed LangChain its provider-agnostic image block (`{"type": "image", "source_type": "url"|"base64", ...}`) and let `ChatOpenAI(use_responses_api=True)` translate to `input_image`, instead of hand-building the Chat-Completions `image_url` dict that bypasses translation. Then images *work* on `gpt-5-nano`.
2. **Model-aware kwargs (cures 1a, already fixed for the one site).** Keep `cache_control` (and any provider-specific kwarg) behind a capability/provider check. Better: fold prompt-caching into `get_llm(...)`/middleware so the one place that knows the provider owns the decision, and call sites stop passing `cache_control` at all. NB: 13 agents still add `cache_control` content blocks *unconditionally* (`rails_agent/nodes.py:67`, `sub_agents.py:172,184`, `pyxl_agent`, `rails_user_mode_agent`, +8) — latent, same class, worth the same treatment.
3. **Conservative capability default (prevents the next unknown).** `get_model_capabilities()` currently defaults an *unknown* model to `{images:True, video:True, pdf:True}` (`model_capabilities.py:44`) — backwards. A new model gets images thrown at it and 400s. Flip the default to text-only; opt models into multimodal explicitly once verified.
4. **Test gate (per CLAUDE.md — bug fix = failing test first).** Fake-LLM e2e that runs the affected agents against a *non-default* model + the triggering content (non-`claude` model for kwargs; a Responses-API model + image for content) and asserts no unexpected-kwarg / no deserialize `400`.

---

## 6. Recommended sequence

**Phase 1 — the 80% (mostly wiring existing primitives; small, low-risk, fail-open):**
1. ✅ **DONE** — Broaden `wrap_model_call` retry from `ResourceExhausted`-only to the model-agnostic transient class (`app/agents/leonardo/resilience.py`), applied to **both** sync and async paths (async had no retry before). Tests: `test_resilience_transient_retry.py`.
2. Add `with_fallbacks([...])` (capability-aware) in the same middleware.
3. Rung-3 graceful floor at `request_handler.py` catch sites — bare DeepSeek call with **content stripped**.
4. ✅ **DONE (method + wiring)** — `MothershipClient.report_error` + wired into all 3 handler catch sites via `RequestHandler._report_error_to_mothership`. Tests: `test_report_error.py`. *Remaining: the [Contact support] button → mothership emails support@llamapress.ai (frontend + mothership endpoint).*

**Implementation status (this branch):** rungs 0 (tool-repair, pre-existing) and 1 (transient retry) are live; error telemetry to the mothership is live for all three chat error paths (`recovered=False` until the graceful floor lands). Not yet built: rung 2 fallback, rung 3 graceful floor, the [Contact support] button, and the §5 cures.

**Phase 2 — cures & mothership (driven by Phase-1 telemetry):**
5. Model-aware image format (§5.1) + conservative capability default (§5.3).
6. Centralize `cache_control` behind provider-awareness (§5.2) + the non-default-model e2e tests (§5.4).
7. Mothership dashboard + alerting (§4 Layer 3).

**Phase 3 — only if telemetry proves the need:** "Try a simpler approach" offered mode-fallback; mothership auto-remediation (flag stale-version instances, volume alerts).

Don't build Phase 3 until Phase 1's telemetry shows where the pain actually is.

---

## Appendix — key references

- Swallow points: `app/websocket/request_handler.py:824-832`, `952-953`, `1160-1161`
- Server log w/ trace: `request_handler.py:825` (`exc_info=True`)
- Frontend render (lost): `app/frontend/chat/websocket/MessageHandler.js:1300-1301`
- Phone-home channel: `app/services/mothership_client.py` (esp. `report_disconnect:281`)
- Instance identity: `.leonardo/instance.json` via `MothershipClient._load_config` (`:27-49`)
- Model retry chokepoint: `app/agents/leonardo/rails_agent/middleware.py:389-400` (`wrap_model_call`)
- Tool-call repair (exists): `app/agents/leonardo/agent_factory.py:112-126` (`build_leonardo_agent`)
- Capability table + preflight: `app/agents/leonardo/model_capabilities.py`; consulted at `request_handler.py:1392`, `middleware.py:279`
- Model wiring (Responses API): `app/agents/leonardo/llm_factory.py:171-184` (`gpt-5-nano`, `use_responses_api=True`)
- `cache_control` guard-fix commit: `e9c45da` (0.3.5n); unconditional content-block sites still latent
</content>
