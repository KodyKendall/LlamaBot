# Operator model gates

Four **operator-only** environment variables control which LLM models an instance
user may reach and whether images may be sent. All are read from the environment
(or the mothership-provisioned `.leonardo/instance.json`) — the instance user has
no write path to any of them. Enforcement lives in
`app/agents/leonardo/model_policy.py` (the authoritative chokepoint is `get_llm`;
the `/api/available-models` endpoint and the frontend only reflect the rules for
UX). Vision is additionally enforced in
`RequestHandler._build_message_content`.

| Variable | Default | Effect |
| --- | --- | --- |
| `MODEL_SWITCHING_ALLOWED` | `false` | When off, the instance is **pinned to `deepseek-v4-flash`** — the model dropdown is hidden and any other requested model is swapped for the default server-side. Set `true` to let users pick models (subject to the allow/disable lists below). |
| `VISION_MODEL_ALLOWED` | `false` | When off, **image/video attachments are refused**. The frontend blocks the send with a `support@llamapress.ai` hand-off; the backend strips the attachment and appends the same note even if the frontend is bypassed. Set `true` to enable image understanding (image sends auto-switch to the vision model). |
| `ENABLED_MODELS` | *(unset)* | Comma-separated allow-list. Only applies when switching is on. If set, only these models (plus the fail-open defaults) are selectable. |
| `DISABLED_MODELS` | *(unset)* | Comma-separated disable-list. Highest precedence — turns a model off even if it is the default or otherwise enabled. |

Truthy values for the two booleans: `1`, `true`, `yes`, `on` (case-insensitive).
Anything else (including unset/blank) is false.

## Resolution order (`is_model_enabled`)

1. **Explicit disable** (`DISABLED_MODELS` / `instance.json` `disabled_models`) — off, beats everything.
2. **Model-switching lock** (`MODEL_SWITCHING_ALLOWED=false`) — only `deepseek-v4-flash` is enabled, plus `gemini-3.1-flash-lite` when `VISION_MODEL_ALLOWED=true` (so the image auto-switch still works).
3. **Fail-open defaults** — `deepseek-v4-flash` + `gemini-3.1-flash-lite` survive any allow-list.
4. **Allow-list** (`ENABLED_MODELS` / `enabled_models`) — restricts everything else; sources intersect.
5. **Inert** — nothing configured → every model enabled (still key-gated).

## Notes

- Changing these vars requires a container **force-recreate**, not a restart —
  the backend does not hot-reload (`up -d --force-recreate llamabot`).
- The two booleans default **off**, so a fresh instance is locked to DeepSeek
  with vision disabled until an operator opts in. To change the fleet-wide
  default, flip `_MODEL_SWITCHING_ALLOWED_DEFAULT` / `_VISION_ALLOWED_DEFAULT`
  in `model_policy.py`.
