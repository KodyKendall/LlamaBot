# Adding an LLM to the model dropdown

Every model offered in the UI must be registered in **five** places. Miss one and the
failure is silent rather than loud — `app/tests/test_model_registry_consistency.py`
turns each miss into a red build, and its module docstring explains each symptom.

| # | File | What to add |
|---|------|-------------|
| 1 | `app/frontend/chat.html` | `<option value="…" data-short-label="…">` inside the `data-llamabot="model-select"` select. `value` must come first — the test regex reads it positionally. |
| 2 | `app/agents/leonardo/llm_factory.py` | a `get_llm` branch building the client. No branch ⇒ silently falls through to the DeepSeek default, so the user picks one model and gets another. |
| 3 | `app/agents/leonardo/model_capabilities.py` | `{'images': …, 'video': …, 'pdf': …}`. Missing ⇒ the permissive default applies, the UI offers image upload, and a text-only provider 400s on `image_url`. |
| 4 | `app/agents/leonardo/model_policy.py` | append to `_KNOWN_MODELS`. |
| 5 | `app/routers/api.py` (`available_models`) | the required env var, or a tuple of them (first found wins). Missing ⇒ the dropdown entry is never marked available/unavailable. |

## Third-party OpenAI-compatible endpoints

Most non-OpenAI providers (GMI, Fireworks, Alibaba, Meta) speak OpenAI-compatible
chat completions, so the client is just `ChatOpenAI` (or `ChatDeepSeek`/`ChatQwen`)
with `base_url` overridden. Two rules:

**1. Never pass a bare `api_key=os.getenv("PROVIDER_KEY")`. Use
`llm_factory.provider_key("PROVIDER_KEY", …)`.**

All of these clients sit on the `openai` SDK, and that SDK falls back to
`OPENAI_API_KEY` from the environment when handed `api_key=None`. With `base_url`
pointed at a third party, an instance that has `OPENAI_API_KEY` but not the
provider's key would send our OpenAI secret in an `Authorization: Bearer` header
**to that provider**. `provider_key` returns a dud placeholder instead, turning a
credential disclosure into an honest 401. Pinned by
`app/tests/test_provider_key_never_leaks_openai.py` — add new endpoints to its
parametrize list.

**2. The system prompt must be flattened.** These gateways require system `content`
to be a plain string and 400 on Anthropic's cache-control block list. That is
already handled centrally: `supports_prompt_caching()` returns False for any
non-Anthropic name, and `system_message_for_model()` flattens accordingly. Don't
re-derive either check at a call site.

## Registering a model is not the same as enabling it

All five registrations can be correct and the model still never runs. `model_policy`
resolves an **allow-list**, and the allow sources **intersect**: `.leonardo/instance.json`
`enabled_models` ∩ `ENABLED_MODELS`. A model absent from either is swapped for the box
default inside `get_llm`, so the dropdown keeps showing the user's pick while every turn
runs on something else. On the dev box, `instance.json` carries a real `enabled_models`
list — add the new name there (`~/dev/Leonardo/.leonardo/instance.json`, read per request,
no restart needed) or nothing you added will be reachable.

The symptom is one log line per turn:

```
WARNING - Requested model 'x' is disabled by policy; using 'muse-spark-1.2-contributor' instead.
```

Since 0.7.0e that substitution also raises a `model_substituted` websocket frame, shown as
a banner above the composer — check the UI before digging through logs. Fleet-wide the
allow-list is mothership-owned, so a model that ships in the dropdown but not in
`enabled_models` is invisible on every real box.

## Verifying on the dev box

`MODEL_SWITCHING_ALLOWED` defaults to False and is unset in the container, so
`get_llm` replaces any non-default model with DeepSeek. Probe with it set,
otherwise you are inspecting a DeepSeek client and won't notice:

```bash
docker exec -e MODEL_SWITCHING_ALLOWED=true -e PROVIDER_API_KEY=test \
  leonardo-llamabot-1 sh -c 'cd /app && python -c "
from app.agents.leonardo.llm_factory import get_llm
m = get_llm(\"your-model\")
print(type(m).__name__, m.model_name, m.openai_api_base)"'
```

## Notes on specific entries

- **`muse-spark-1.2-contributor`** (Meta) — the tier lives entirely in the model id.
  `muse-spark-1.2` is $1.25/$4.25 per 1M tokens and is not trained on;
  `muse-spark-1.2-contributor` is $0.10/$0.20 and licenses Meta to train on every
  prompt and completion sent to it, including customer application code. We ship the
  contributor id deliberately; an operator can move a single instance to the paid,
  non-training tier with `META_MUSE_MODEL=muse-spark-1.2`, no code change. Note this
  is the opposite trade from `deepseek-v4-flash-fireworks`, which exists precisely to
  keep customer code out of a third party's training data — so don't make Muse a
  fleet default without revisiting that decision. Key: `META_API_KEY` (Meta's docs
  call it `MODEL_API_KEY`; both are accepted).

  **Muse shows no thinking tokens, and that is Meta's behavior, not our bug.** The
  API reasons and bills for it (`usage` reports 250–700 `reasoning_tokens` on small
  prompts) but never returns the reasoning text. Verified 2026-08-08 against the
  live API on all four paths: chat completions returns a message with only
  `content`/`role` and no `reasoning_content` field; the Responses API returns a
  `reasoning` item with `summary: []`, `content: null` and no `encrypted_content`,
  under `summary` of `auto`, `detailed` **and** `concise`; the streaming deltas carry
  only `content`/`role`; and the standard `muse-spark-1.2` tier behaves identically
  to the contributor tier. So there is nothing for `request_handler`'s extractor to
  pick up — do not "fix" it by adding a reasoning shape for Meta, and don't reach for
  `use_responses_api=True` expecting summaries the way the GPT-5 entries get them.

- **`nemotron-lightning-30b-fireworks`** — the same weights as the self-hosted
  `nemotron-lightning-30b-runpod`, on Fireworks serverless, and a sibling entry rather
  than a re-point of it. Verified live 2026-08-16: Fireworks returns Nemotron's thinking
  in a separate `reasoning_content` field (streamed as deltas, and accepted back on
  assistant messages), so it uses `ChatDeepSeekWithReasoning` — a bare `ChatOpenAI`
  would drop the thinking. It sets **no** `max_tokens`: reasoning bills against that cap
  while being stripped from the response, so a small cap returns empty content with no
  error, and Fireworks imposes no small default of its own. Key: `FIREWORKS_API_KEY`,
  falling back to the `FIREWORKS_DEEPSEEK_API_KEY` name already deployed on boxes (one
  Fireworks account issues one key, so a per-model key name would be fiction).

- **`qwen3.8-27b-hetzner`** — Qwen3.8-27B (dense) on **Hetzner's Inference API**
  (`https://inference.hetzner.com/api/v1`), an EU-hosted OpenAI-compatible gateway, so
  the client is a plain `ChatOpenAI` with an overridden `base_url` — not `ChatQwen`,
  which exists for Alibaba DashScope's reasoning shape. Key: `HETZNER_API_KEY`.
  262k context, text + image in. Hetzner serves a small, changing set of models and
  `/v1/models` is the definitive list; `HETZNER_QWEN_MODEL` re-points this entry to
  their other one (`Qwen/Qwen3.6-35B-A3B-FP8`, MoE, 35B total / 3B active, same context
  and modalities) with no code change, and `HETZNER_BASE_URL` overrides the endpoint.

  **The rate limit is on requests, not tokens: 10 per 60s per key** (against 4M in /
  100k out tokens per 60s). One agentic Leo turn is many sequential requests, so a
  single busy user can exhaust it. 429s are classified transient and retried by the
  resilience middleware, so they surface as slow turns rather than failed ones — but
  it is why this must not become a fleet default while the API is free/experimental.

  Not yet verified against the live endpoint (no `HETZNER_API_KEY` on the dev box as
  of 2026-08-30). Two things to check on first use: whether tool calling works at all
  through their vLLM serve flags, and whether thinking arrives inline as
  `<think>…</think>` inside `content` — if it does, `chat_template_kwargs=
  {"enable_thinking": False}` (as on the RunPod Qwen3-8B entry) is the first knob.
