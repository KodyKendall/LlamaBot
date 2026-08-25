# Mid-turn Rails auto-recovery (0.7.4)

When agent-written code crashes the Rails app, the user should never be the one
who discovers it. Leo notices during the same turn and fixes it before handing
back.

## Shape

```
Rails exception  ──▶ MothershipReporter.report_exception   (already the single funnel:
                          │                                 rack middleware, Rails.error
                          │                                 subscriber, Leonardo error page)
                          ├──▶ mothership telemetry  (unchanged)
                          └──▶ LlamaBotRails::ErrorLog   ring buffer, in memory
                                        │
                                        ▼
                          GET /llama_bot/errors?since=<seq>   (agent-token gated)
                                        │
                    ┌───────────────────┘  polled once per model call
                    ▼
LlamaBot  RailsErrorWatchMiddleware.awrap_model_call
                    │
                    └──▶ appends one `[automated]` HumanMessage, then calls the handler
```

Pull, not push. Rails does not know LlamaBot's URL; LlamaBot already knows
Rails' (`RAILS_BASE_URL`). Polling also catches crashes in background jobs and
in requests the user's browser never made — a push from the preview iframe only
sees pages that iframe happened to load.

The model-call boundary is the injection point because it is the only place
LangGraph lets you add a message to a live run without cancelling and restarting
it, and it is the point the model actually reads.

## Why these guardrails

The failure mode this feature invites is a fix→break→fix loop that burns a
thread. Every rail below exists to bound that:

| Rail | Effect |
| --- | --- |
| Arming call looks back `ARMING_WINDOW_SECONDS` (120s), then the cursor takes over | Catches the common case — the app was ALREADY broken when the user asked — without replaying the whole ring. v1 primed to *now* and so was structurally silent for exactly that case. |
| `MAX_INJECTIONS_PER_TURN = 3` | Fourth new error injects a "stop and explain to the user" note instead, then the watch goes quiet. |
| Fingerprint dedup per turn | A render loop firing the same `NoMethodError` 200× is mentioned once. |
| `MAX_REPORT_CHARS = 4000` | A 40-frame backtrace cannot blow the context window. |
| Plan-mode agents disarmed | Read-only modes are not allowed to act on it. |
| Every failure path returns `None` | Old gem, timeout, expired token, malformed JSON — the turn proceeds untouched. |

## Auth

`Authorization: LlamaBotFeed <hmac>`, where both sides compute
`HMAC-SHA256(SECRET_KEY_BASE, "llamabot-error-feed")`. Both containers are handed
the same `SECRET_KEY_BASE` from the same `.env`, so nothing has to be provisioned.
Plain HMAC rather than ActiveSupport's `MessageVerifier` because the other end is
Python and Marshal-based signing cannot be reproduced there.

**This is the second design.** The first authenticated as the signed-in Rails
user, reusing the `api_token` the chat frontend puts on every WebSocket frame.
That token only exists while the human holds a Devise session in the browser tab,
so the whole feature silently no-opped for anyone signed out — the log line was
`no Rails api_token on the frame`. Whether the box can read its OWN error log
must not hinge on a browser session. The per-user token survives only as a
fallback for an ejected app whose Rails container does not share the secret.

The endpoint returns 403 to anything else, so backtraces stay private.

## Kill switch

`LLAMA_BOT_ERROR_FEED=false` in the Rails container turns the ring buffer off
(the endpoint then returns an empty feed). Independent of
`LLAMA_BOT_ERROR_TELEMETRY`, which governs mothership reporting only.

---

# TDD plan

Each component is unit-testable in isolation. Write the test, watch it fail,
implement, watch it pass.

## Gem — `llama_bot_rails` (rspec)

### 1. `LlamaBotRails::ErrorLog` — `spec/lib/llama_bot_rails/error_log_spec.rb`

| # | Test | Asserts |
| --- | --- | --- |
| 1 | records class, message, method, path, backtrace | entry fields populated from the exception + rack env |
| 2 | assigns strictly increasing `seq` | second entry's seq > first |
| 3 | `since(seq)` returns only newer entries | cursor semantics |
| 4 | `since(latest_seq)` returns `[]` | no re-delivery |
| 5 | caps at `MAX_ENTRIES` | oldest fall off, buffer never grows |
| 6 | repeat of the newest fingerprint collapses | one entry, `count: 2`, fresh `seq` |
| 7 | a *different* fingerprint does not collapse | two entries |
| 8 | nil backtrace / no env does not raise | entry still recorded |
| 9 | disabled by `LLAMA_BOT_ERROR_FEED=false` | `record` no-ops, `since` returns `[]` |
| 10 | `record` swallows internal failure | returns nil, never raises |

### 2. Reporter tee — `spec/lib/llama_bot_rails/mothership_reporter_error_log_spec.rb`

| # | Test | Asserts |
| --- | --- | --- |
| 1 | records locally even when mothership is **not configured** | local recovery does not depend on telemetry creds |
| 2 | records locally even when **throttled** | throttle is a bandwidth rail, not a recovery rail |
| 3 | `ignorable?` exceptions (404 / RoutingError) are **not** recorded | no false alarms from bot scans |
| 4 | same exception object reported twice records **once** | dedup marker independent of the telemetry marker |
| 5 | mothership dispatch is unchanged when configured | tee is additive, existing telemetry spec still green |

### 3. Endpoint — `spec/controllers/llama_bot_rails/errors_controller_spec.rb`

| # | Test | Asserts |
| --- | --- | --- |
| 1 | no `Authorization` header → 403 | backtraces are not public |
| 2 | valid agent token → 200 JSON `{seq:, errors: []}` | happy path |
| 3 | no `since` param → `errors` empty, `seq` current | cursor probe |
| 4 | `?since=N` → only entries with `seq > N` | delta fetch |
| 5 | non-numeric `since` → treated as probe, no 500 | hostile input |

## LlamaBot (pytest, run in the container)

### 4. Pure turn logic — `app/tests/test_rails_error_watch.py`

Tests `RailsErrorWatch.plan_injection()` — no LangGraph, no HTTP, no event loop.

| # | Test | Asserts |
| --- | --- | --- |
| 1 | no new errors → `None` | quiet turn stays quiet |
| 2 | one new error → text contains `[automated]`, class, message, path | the model gets what it needs |
| 3 | same fingerprint twice → second call `None` | per-turn dedup |
| 4 | three distinct errors → three injections | budget spends correctly |
| 5 | fourth error → give-up text, `injections` frozen | loop bound |
| 6 | fifth error after give-up → `None` | give-up delivered once |
| 7 | huge backtrace → text ≤ `MAX_REPORT_CHARS` | context safety |
| 8 | two errors in one poll → single message listing both | no burst of messages |
| 9 | `agent_can_auto_recover("rails_plan_mode_agent")` is False | mode gate |
| 10 | `agent_can_auto_recover("rails_agent")` is True | mode gate, positive case |
| 11 | watch with no `api_token` is not armed | graceful degradation |

### 5. Feed client — `app/tests/test_rails_error_feed_client.py`

Driven by an injected `httpx.MockTransport`; no network.

| # | Test | Asserts |
| --- | --- | --- |
| 1 | 200 with valid body → `(seq, errors)` | happy path |
| 2 | sends `Authorization: LlamaBot <token>` | auth wiring |
| 3 | `since=None` → no `since` query param | cursor probe |
| 4 | `since=7` → `?since=7` | delta fetch |
| 5 | 403 → `None` | expired token degrades silently |
| 6 | 404 → `None` | old gem without the endpoint |
| 7 | timeout / connect error → `None` | Rails down mid-turn |
| 8 | malformed JSON → `None` | never raises into the turn |
| 9 | body missing `seq` → `None` | contract violation is not a crash |

### 6. Middleware — `app/tests/test_rails_error_watch_middleware.py`

Fake request + fake handler; asserts structure only.

| # | Test | Asserts |
| --- | --- | --- |
| 1 | no active watch → handler called with request untouched | inert in headless runs and tests |
| 2 | first call primes the cursor and injects nothing | pre-turn errors ignored |
| 3 | new error on a later call → one message appended before the handler runs | the actual feature |
| 4 | handler's response returned unchanged | transparent |
| 5 | client raising → handler still called, no exception escapes | never fatal |
| 6 | disarmed watch (plan mode) → no poll attempted | mode gate honoured at the boundary |
| 7 | `build_leonardo_agent` includes the middleware exactly once | wiring, idempotent |

## Manual QA

Lives in `docs/test_plans/0.7.4.md` — the loop that cannot be unit tested is
"Leo actually fixed it", which needs a real model.
