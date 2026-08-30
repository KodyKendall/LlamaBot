# Adding an OpenRouter model (no code change)

Every first-party model in LlamaBot costs six code edits to add — the dropdown,
`get_llm`, `MODEL_CAPABILITIES`, `_KNOWN_MODELS`, the api-key map and the label
map. `test_model_registry_consistency` exists to make missing one a red build.

OpenRouter does not fit that shape. It is one OpenAI-compatible endpoint in front
of hundreds of models, and each model is served by many *provider endpoints* that
differ only in price, quantization and throughput — `deepseek/deepseek-v4-flash-0731`
alone has 28. Trying one is a pricing experiment, not a code change, so they are
config: **`.leonardo/openrouter_models.json`**.

## The file

```json
{
  "models": {
    "deepseek-flash-0731-openinference": {
      "label": "DeepSeek V4 Flash 0731 (OpenInference fp4)",
      "short_label": "DS 0731 OI",
      "model": "deepseek/deepseek-v4-flash-0731",
      "provider": { "order": ["open-inference/fp4"], "allow_fallbacks": false },
      "capabilities": { "images": false, "video": false, "pdf": false }
    }
  }
}
```

The key (`deepseek-flash-0731-openinference`) is the name the frontend sends and
the policy gates on. That block is the entire registration: it feeds `get_llm`,
the capability table, the policy's known-models list, the api-key map and the
dropdown. Restart is not required — the file is re-read on each call, so you can
edit it and reload the chat page.

| Field | Required | Meaning |
|---|---|---|
| `model` | **yes** | The OpenRouter model id. An entry without one is dropped with a warning — it could not build a client. |
| `label` / `short_label` | no | Dropdown text. Defaults to the entry key. `short_label` is what inline notices use. |
| `provider` | no | OpenRouter provider routing, verbatim. **Pin it** — see below. |
| `capabilities` | no | `images` / `video` / `pdf`. Defaults to **text-only**. |
| `enabled` | no | `false` parks the entry (keep the notes, hide the model). |
| `reasoning` | no | `false` builds a plain `ChatOpenAI` instead of the reasoning-aware client. |
| `extra_body` | no | Escape hatch for any other top-level OpenRouter request field. |

## Always pin the provider

Without a `provider` block, OpenRouter load-balances across every endpoint for
that model. Two things break:

- **Price.** `relace/fp4` is $0.040/$0.080 per M tokens; the same model routed to
  DeepSeek direct is $0.44/$1.32. A silent reroute erases the reason you picked it.
- **Prompt caching.** Cache hit rate is per-replica. A request that lands on a
  different provider each turn caches nothing.

`allow_fallbacks: false` means a provider outage is a **failed turn**, not a
silent switch to a pricier endpoint. That is the intended default here. Flip it
to `true` for a box that would rather pay than fail.

Find the endpoint slugs for a model with:

```bash
curl -s https://openrouter.ai/api/v1/models/<author>/<slug>/endpoints \
  | python3 -c "import json,sys; [print(e['tag'], e['quantization'], e['pricing']['prompt']) for e in json.load(sys.stdin)['data']['endpoints']]"
```

## What ships compiled in

Three entries, all smoke-tested (tool calling, reasoning, streaming, through the
pin). They are siblings, not alternatives — who serves and bills for a turn stays
an explicit user choice:

| Entry | Model | Endpoint | In/M | Out/M | Cache read/M | Uptime |
|---|---|---|---|---|---|---|
| `deepseek-flash-0731-relace` | DS V4 Flash 0731 | `relace/fp4` | $0.040 | $0.080 | $0.0080 | 97.94% |
| `deepseek-flash-0731-digitalocean` | DS V4 Flash 0731 | `digitalocean` | $0.080 | $0.252 | $0.0252 | 99.06% |
| `glm-5.3-flash-zai` | GLM 5.3 Flash | `z-ai/fp8` | $0.075 | $0.250 | $0.0150 | 99.44% |

For reference, the same model at DeepSeek direct is $0.44/$1.32. Relace is the
cheap one; DigitalOcean costs ~2-3x more and buys better uptime and a named US
host. With `allow_fallbacks: false`, a provider's downtime is a failed turn, so
uptime is not a footnote.

> **Measured caveat (2026-08-26): DigitalOcean's tool calling is unreliable.**
> Against the real 22.8k-token agent prompt it invented tool names that were
> never bound (`Read`, `exec_command`, `list_files`, `exec`) in 4 of 9 turns, and
> once emitted its raw tool-call template as plain text instead of a structured
> call — a serving-side tool-parser defect. Relace scored 12/12 correct tool
> choice with zero invented names and was ~5x faster. Relace's own weakness is
> availability: it 502s under queue pressure (`Queued past the 5s queue-time
> bound`), which `allow_fallbacks: false` turns into a failed turn. Those 502s
> are now classified transient and retried (see `resilience._status_code_of`).
>
> **Single probes do not surface either problem.** Measure a new endpoint at
> volume, against a real prompt with real tools bound, before trusting it.

`glm-5.3-flash-zai` (added 2026-08-27) is a different model on the same routing.
Probed the same way — 12 turns, real prompt, real tools — it scored **12/12
correct tool choice, zero invented names, zero template leaks**, matching Relace
and unlike DigitalOcean. Two things distinguish it:

- **Prompt caching lands hard.** 22,208 of 22,250 input tokens returned as cache
  reads, billed at $0.015/M instead of $0.075/M. On a steady-state Leo turn that
  is most of the input cost gone.
- **It is slower.** ~7s/turn vs 1.6s on Relace, with ttft ~5s. It is a reasoning
  model and that is thinking time (well inside the 25s stall detector), but pick
  Relace for a latency-sensitive box.

It is also **1M+ context** and the only compiled-in entry that can see images —
verified through the pin, not assumed. Jurisdiction is the open question: `z-ai/fp8`
is a first-party Chinese host, and the same weights are served from the US by
`baseten/fp8` and `cloudflare` at $0.15/$0.50. Changing `order` in the overlay
moves it without a rebuild.

**Quantization tells you less than it looks.** Relace is explicitly `fp4`;
DigitalOcean reports `unknown`, which is unreported metadata rather than a claim
of higher precision — 10 of this model's 29 endpoints report `unknown`, including
DeepSeek's own and Fireworks. Assume nothing from the field; if fp4 output
quality matters for a task, test it.

Neither appears on a box without an `OPENROUTER_API_KEY`.

## Credentials and policy

One key for everything: `OPENROUTER_API_KEY`. `OPENROUTER_BASE_URL` overrides the
endpoint (a proxy, or a mock in tests).

**Registering a model enables it.** The file is operator-owned and host-mounted,
the same trust tier as `instance.json` and `ENABLED_MODELS`, so the act of adding
a block is the operator saying "offer this" — requiring `ENABLED_MODELS` as well
would be configuring one intent twice. Still true:

- `DISABLED_MODELS` wins over a registration.
- `MODEL_SWITCHING_ALLOWED=false` still pins the box to its default model.
- A registered model is never chosen as a box's *default*; it is a thing a user
  picks.

## Gotchas

- **Capabilities default to text-only**, the opposite of the unknown-model default
  elsewhere. Declaring `images: true` for a model that cannot see them offers an
  upload that 400s at the provider, so it is opt-in.
- **Probe vision with a realistic image.** An 8x8 test PNG is rejected by Z.AI
  with `400 code 1210 图片输入格式/解析错误` — indistinguishable from "this
  endpoint has no vision path". The same model describes a 256x256 PNG correctly.
  A tiny probe image will talk you out of a capability the model actually has.
- **A malformed file is ignored, not fatal** — you get the compiled-in base and a
  warning in the container logs. Check the logs if a model you added never shows up.
- **Reasoning survives the routing** — verified on both compiled-in entries:
  `reasoning_content` arrives populated and streams as deltas, so thinking renders
  the same as on DeepSeek direct. Do not assume that for a *new* endpoint though;
  it is the first thing to check, and `"reasoning": false` is the escape hatch.
- **Third data processor.** A routed request goes to OpenRouter *and* the chosen
  provider. The move to Fireworks for DeepSeek was made on jurisdiction grounds;
  routing re-opens that question with a different set of names.
