# Mothership handoff — performance telemetry ingest

**From:** LlamaBot (shipping now)
**To:** LlamaPress.ai mothership
**Design notes:** `docs/dev/performance_telemetry.md`

LlamaBot instances now measure where each chat turn's wall clock went and post
it. Until the mothership side lands, boxes get a 404 and drop the data — this is
harmless (logged at debug, fail-open) but it is all being thrown away.

Two pieces of work, independent of each other.

---

## 1. `timings` on the existing `report_message` (small)

`POST /api/leonardo/report_message` may now carry an extra key on
`role="assistant"` messages:

```json
"timings": {
  "duration_ms": 5000,
  "ttft_ms": 2000,
  "tokens_per_second": 100.0,
  "model": "deepseek-v4-flash"
}
```

`ttft_ms` may be `null` (non-streaming call); `tokens_per_second` may be `null`
(no measurable decode time). Suggested storage: one jsonb column on
`InstanceMessage` next to the existing token-usage data.

Value: a per-message tokens/sec series per model, which is how we would notice
a provider degrading before customers tell us.

---

## 2. New endpoint `POST /api/leonardo/report_turn_metrics`

Auth and shape match `report_error` — `Authorization: Bearer <instance token>`,
`instance_name` in the body.

```json
{
  "instance_name": "leo-acme",
  "thread_id": "abc-123",
  "agent_mode": "rails_agent",
  "model": "deepseek-v4-flash",
  "llamabot_version": "0.6.1",
  "occurred_at": "2026-08-08T00:00:00+00:00",
  "metrics": {
    "total_ms": 12480,
    "ttft_ms": 1830,
    "model_ms": 9200,
    "tool_ms": 2600,
    "overhead_ms": 680,
    "model_calls": 3,
    "tool_calls": 4,
    "output_tokens": 742,
    "input_tokens": 48120,
    "tokens_per_second": 96.4,
    "slowest_tool": {"name": "browser_inspect", "ms": 2100}
  }
}
```

Field notes for whoever builds the table:

- `total_ms = model_ms + tool_ms + overhead_ms`, always. `overhead_ms` is
  clamped at 0 (concurrent tools can out-sum the wall clock), so treat the
  identity as approximate when `tool_calls > 1`.
- `ttft_ms` is the **turn's** first content frame, not the first model call's.
- `tokens_per_second` excludes time-to-first-token for streaming calls and
  includes it for the raw-node agents (beginner / ai_builder / plain chat),
  which use blocking invokes. `input_tokens` is shipped alongside so prefill can
  be regressed out.
- Every field except `metrics` may be absent. `metrics` is never empty (the box
  skips the post entirely rather than send an empty rollup).
- **Errored and cancelled turns are included.** Do not filter them out — they
  are the slowest ones. There is no success flag today; if the dashboard needs
  to split them, say so and we will add one.

### What we want to see on the dashboard

Per instance and fleet-wide, over time: p50/p95 of `ttft_ms` and `total_ms`,
median `tokens_per_second` **broken down by model**, and the
`model_ms / tool_ms / overhead_ms` split as a stacked series. Plus `input_tokens`
against thread depth — that one tests the long-thread hypothesis directly.

### Ingest volume

One request per turn, ~200 bytes. Negligible against the existing
`report_message` traffic, which already fires **one request per ToolMessage
carrying untruncated tool output** — on a tool-heavy engineer turn that is
dozens of requests with file contents and browser dumps in them. If ingest cost
is a concern, that is the thing to look at, not this.

---

## Known mothership-side issue this shares with agent friction

`report_error` allowlists `source` to `%w[llamabot rails_app frontend]` and
silently rewrites anything else to `"llamabot"` — which is why agent-friction
rows currently land mislabelled. Performance telemetry deliberately avoids that
trap by using its own endpoint rather than tunnelling through `report_error`.
