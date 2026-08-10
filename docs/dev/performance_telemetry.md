# Performance Telemetry — measuring why a turn was slow

**Status:** Shipped (LlamaBot side). Mothership ingest: see `docs/handoff_mothership_performance.md`.
**Scope:** LlamaBot. The dashboard/storage contract is specified here but built on the mothership.

---

## 0. TL;DR

We shipped rich *token* telemetry to the mothership and not one *duration*. So
"Leo is slow" arrived as a support ticket with no way to tell apart three very
different diseases:

1. the provider is decoding slower (tokens/sec down),
2. the prompt got huge on a long thread, so time-to-first-token grew with it,
3. the box is the bottleneck — tools, the graph loop, checkpointer writes.

A single "this turn took 40s" number cannot separate those. So we record the
turn as **segments** and derive the two rates that matter.

Every turn now ships:

```
total_ms  =  model_ms  +  tool_ms  +  overhead_ms
             ^^^^^^^^     ^^^^^^^     ^^^^^^^^^^^
             provider     our tools   graph, checkpointer, serialization, us
```

plus `ttft_ms` (what the user actually waits for), `tokens_per_second`
(measured over decode time only), and `input_tokens` (the long-thread signal).

---

## 1. What gets measured, and where

| Signal | Where it is measured | Covers |
|---|---|---|
| model wall time, per call | `TurnMetricsMiddleware.awrap_model_call` | all 11 `create_agent` modes |
| model wall time, raw agents | `resilience.invoke_with_transient_retry` | beginner, ai_builder, plain chat |
| **TTFT per model call** | callback handler attached to the model for that call | streaming `create_agent` calls |
| **TTFT for the turn** | `request_handler`, first *content* frame sent to the browser | every turn |
| tool wall time, by name | `TurnMetricsMiddleware.awrap_tool_call` | all `create_agent` modes |
| turn wall clock | `request_handler`, around the whole `astream` | every turn |
| peak prompt size | token usage on each model call | every turn |

The recorder itself (`app/lib/turn_metrics.py`) is a plain mutable object with
no I/O, reached through a `ContextVar`. `start_turn()` installs it in the
request handler; LangGraph's node tasks inherit a copy of the context and so
resolve the var to the *same* object. `current_turn()` returns `None` when
nothing installed a recorder (headless executor, scheduled jobs, tests) and
every instrumentation point degrades to a pass-through.

### Two arithmetic decisions worth knowing

**tokens/sec is measured over decode time, not the whole call.** Time spent
waiting for the first token is queueing and prefill, not generation. Folding it
in makes a healthy model look slow on a long prompt — the exact confusion this
module exists to remove. Raw-agent calls use blocking `.invoke()` and have no
observable first token, so their rate *does* include prefill; `input_tokens`
travels alongside so the two can be separated downstream.

**`overhead_ms` clamps at zero.** Tools run concurrently, so summed tool time
can legitimately exceed the turn's wall clock.

---

## 2. Reading the numbers

The first real measurement on the dev box, asking a fresh thread to reply with
one word:

```
total=3558ms ttft=3494ms model=1896ms tool=0ms overhead=1662ms tok/s=22.68 input_tokens=28071
```

Three things fall out of one line, none of which we could see before:

- **`input_tokens=28071` before the user typed anything.** The system prompt and
  tool schemas are the floor under every turn's prefill cost. This is the number
  that grows on a long thread and drags TTFT with it.
- **`overhead_ms=1662`** — 47% of the turn spent neither waiting on the provider
  nor running a tool. On an idle box, with a trivial prompt. This is where the
  "the LangGraph/checkpoint loop is the problem" hypothesis gets tested.
- **`ttft` (3494ms) exceeds `model_ms` (1896ms)** — so ~1.6s elapsed *before the
  model was even called*: graph setup, state load, middleware, summarization
  token counting.

Rules of thumb when triaging from the dashboard:

| Pattern | Reading |
|---|---|
| `tokens_per_second` down fleet-wide, `overhead_ms` flat | provider-side degradation |
| `ttft_ms` up, `input_tokens` up, tok/s flat | long-thread prefill — a summarization/context problem, not a speed problem |
| `overhead_ms` up, model and tool flat | the box: graph loop, checkpointer, DB pool |
| `tool_ms` dominant, `slowest_tool` consistent | one tool is the whole complaint |

---

## 3. What it costs the mothership

Deliberately **one extra request per turn**, not per event. This matters
fleet-wide, and the honest comparison is with what we already send:

| Channel | Requests per turn | Payload |
|---|---|---|
| `report_message` (user) | 1 | the message |
| `report_message` (assistant) | one per top-level AI message | the reply |
| `report_message` (tool) | **one per ToolMessage** | **tool output, untruncated** |
| `report_turn_metrics` | **1** | ~200 bytes of scalars |

On a tool-heavy engineer turn the existing tool-output firehose is dozens of
requests carrying the full text of every file read and browser dump. The
metrics rollup is a rounding error next to it, and per-message `timings` adds
zero requests (three scalars on a payload that already exists).

**The pre-existing cost is the one worth attention**, and it is not this
feature: `MothershipClient` opens a **new `httpx.AsyncClient` per report**, so
every one of those requests pays a fresh TLS handshake; tool outputs go
verbatim with no cap; and the reports are `asyncio.create_task` with no
reference held and no in-flight ceiling, so a slow mothership turns into
unbounded queued payloads in the box's memory. See §5.

---

## 4. Contract

Per-assistant-message, on the existing `report_message` payload:

```json
"timings": { "duration_ms": 5000, "ttft_ms": 2000, "tokens_per_second": 100.0, "model": "deepseek-v4-flash" }
```

End of turn, `POST /api/leonardo/report_turn_metrics`:

```json
{
  "instance_name": "leo-acme",
  "thread_id": "abc-123",
  "agent_mode": "rails_agent",
  "model": "deepseek-v4-flash",
  "llamabot_version": "0.6.1",
  "occurred_at": "2026-08-08T00:00:00+00:00",
  "metrics": {
    "total_ms": 12480, "ttft_ms": 1830,
    "model_ms": 9200, "tool_ms": 2600, "overhead_ms": 680,
    "model_calls": 3, "tool_calls": 4,
    "output_tokens": 742, "input_tokens": 48120,
    "tokens_per_second": 96.4,
    "slowest_tool": {"name": "browser_inspect", "ms": 2100}
  }
}
```

Both are fire-and-forget and fail open. A 404 (mothership not upgraded yet) is
logged at **debug** so an un-upgraded fleet cannot spam logs. The rollup is sent
from a `finally`, so **errored and cancelled turns are reported too** — those
are the slow ones users complain about, and a dataset of only clean turns would
flatter us.

---

## 5. Not done yet

- **Resource heartbeat.** RSS, active run count, open websocket count and
  `checkpointer_pool.get_stats()` sampled every 60s on the existing
  `LeaseManager` loop. This is what would confirm or kill the "RAM / multiple
  tabs" hypothesis directly, and pool-wait time is a known way this box wedges
  (SI#137). Needs its own endpoint.
- **Telemetry's own cost, fixed.** A shared `httpx.AsyncClient` (connection
  reuse instead of a handshake per report), a cap on reported tool-output size,
  and a bounded in-flight set for the fire-and-forget tasks. Helps both sides:
  less event-loop and memory pressure on the box, less ingest load on the
  mothership.
- **Client-side timing.** Send → first rendered token, measured in the browser,
  plus tab count. The existing `/api/frontend-error` path is the model for it.
