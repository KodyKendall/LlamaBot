"""Config-driven OpenRouter model registry.

Every other model in the fleet costs six code edits to add — the dropdown,
``get_llm``, ``MODEL_CAPABILITIES``, ``_KNOWN_MODELS``, the api-key map and the
label map (``test_model_registry_consistency`` exists to make missing one a red
build). That is the right shape for a model with its own client, its own quirks
and its own reasons to exist.

It is the wrong shape for OpenRouter. OpenRouter is one OpenAI-compatible
endpoint in front of hundreds of models and, for each model, dozens of *provider
endpoints* that differ only in price, quantization and throughput —
``deepseek/deepseek-v4-flash-0731`` alone is served by 28 of them. Trying one is
a pricing experiment, not a code change, so they live in config instead:

    {
      "models": {
        "deepseek-flash-0731-relace": {
          "label": "DeepSeek V4 Flash 0731 (Relace fp4)",
          "short_label": "DS 0731 Relace",
          "model": "deepseek/deepseek-v4-flash-0731",
          "provider": {"order": ["relace/fp4"], "allow_fallbacks": false},
          "capabilities": {"images": false, "video": false, "pdf": false}
        }
      }
    }

Adding an endpoint is that block. The registry feeds ``get_llm``, the capability
table, the policy's known-models list, the api-key map and the dropdown, so a
registered model is fully wired the moment the file is read.

**Layering** mirrors :mod:`app.lib.langgraph_registry`: a platform base compiled
in below, then a host-mounted client overlay that survives container recreates.
Overlay entries win on a name collision, so a box can re-point or re-price a
built-in entry without a rebuild.

**Ownership and trust.** The overlay is OPERATOR-owned, exactly like
``instance.json`` and ``ENABLED_MODELS`` — it is mounted from the host, and no
agent tool writes to it (the AI-builder's write target is ``langgraph.local.json``
specifically). That is what lets a registered model be *enabled* by default:
requiring the operator to also name it in ``ENABLED_MODELS`` would mean
configuring the same intent twice, which is the friction this file removes. An
explicit ``DISABLED_MODELS`` entry still wins, and ``"enabled": false`` turns one
off in place.

**Fail-open everywhere**: a missing, unreadable or malformed overlay is logged
and skipped. A broken config file must never take the dropdown down.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The OpenAI-compatible endpoint. Overridable for a proxy/mock in tests.
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
API_BASE_ENV = "OPENROUTER_BASE_URL"

# One key for every entry — that is the point of routing through OpenRouter.
API_KEY_ENV = "OPENROUTER_API_KEY"

# Host-mounted client overlay. Sits beside instance.json in the .leonardo mount so
# it survives a container recreate and stays off the platform sync allowlist.
OVERLAY_PATH_ENV = "OPENROUTER_MODELS_CONFIG"
DEFAULT_OVERLAY_PATH = ".leonardo/openrouter_models.json"

# Capability defaults for an entry that declares none. Text-only on purpose —
# the opposite of get_model_capabilities' permissive unknown-model default,
# because here we are reading a config file the operator just wrote: guessing
# "probably multimodal" would offer an image upload that 400s at the provider.
_DEFAULT_CAPABILITIES = {"images": False, "video": False, "pdf": False}

# Compiled-in base entries. Deliberately small: endpoints we have actually
# smoke-tested (tool calling + reasoning + streaming through the pin), not a
# catalogue of the 28 that exist. Everything else belongs in the overlay.
#
# Both cost nothing on a box without an OPENROUTER_API_KEY: policy step 2b gates
# registration on the key, so a fleet box never grows a dropdown option it
# cannot run.
_BASE_MODELS = {
    "deepseek-flash-0731-relace": {
        "label": "DeepSeek V4 Flash 0731 (Relace fp4)",
        "short_label": "DS 0731 Relace",
        "model": "deepseek/deepseek-v4-flash-0731",
        # Pin ONE provider endpoint with fallbacks off. Without this, OpenRouter
        # load-balances across all 28 endpoints for this model, and the whole
        # reason to pick relace/fp4 ($0.04/$0.08 per M, vs $0.22/$0.66 at
        # DeepSeek direct) evaporates on the first silent reroute. It also keeps
        # prompt caching meaningful: cache hit rate is per-replica, so a request
        # that lands on a different provider each turn caches nothing.
        #
        # allow_fallbacks:false means a Relace outage is a FAILED TURN, not a
        # silent switch to a pricier endpoint. That is the intended trade —
        # flip it to true in the overlay if a box would rather pay than fail.
        "provider": {"order": ["relace/fp4"], "allow_fallbacks": False},
        "capabilities": {"images": False, "video": False, "pdf": False},
    },
    "deepseek-flash-0731-digitalocean": {
        "label": "DeepSeek V4 Flash 0731 (DigitalOcean)",
        "short_label": "DS 0731 DO",
        "model": "deepseek/deepseek-v4-flash-0731",
        # Same weights as the Relace entry above, different host. Costs more
        # ($0.08/$0.252 per M vs $0.04/$0.08) and buys two things:
        #
        #   * Uptime. 99.06% vs 97.94% over the last day when this was added.
        #     With allow_fallbacks off, a provider's downtime is our failed turn.
        #   * A named US host. Routing already adds OpenRouter as a processor;
        #     DigitalOcean is at least a jurisdiction we can point at, which is
        #     the same reasoning that moved deepseek-v4-flash to Fireworks.
        #
        # !! TOOL CALLING IS UNRELIABLE HERE — do not point a Leo box at this
        # without re-measuring. A single probe passes (it returns a valid
        # tool_call), which is exactly why this needs the warning: the problem
        # only shows up at volume. Measured against the real 22.8k-token
        # RAILS_AGENT_PROMPT with the real tool set, 2026-08-26:
        #
        #   * invented tool names that were never bound — `Read`,
        #     `exec_command`, `list_files`, `exec` — in 4 of 9 sequential turns,
        #   * one turn emitted its raw tool-call template as PLAIN TEXT
        #     (`<|DSML|...:invoke name="grep">`) instead of a structured call,
        #     which points at a broken tool-call parser on the serving side —
        #     the same class of defect as a vLLM missing --tool-call-parser,
        #   * ~9s/turn, vs 1.6s on the Relace entry above.
        #
        # By contrast the fp4 Relace entry scored 12/12 correct tool choice with
        # zero invented names, so this is NOT a quantization story — quantization
        # is unreported here and fp4 there. It is how this host serves the model.
        #
        # Kept registered because an operator asked for it and it is a valid
        # choice for non-tool-calling use; flip "enabled": false in the overlay,
        # or DISABLED_MODELS, to take it off the dropdown.
        #
        # Quantization is reported "unknown", which is NOT a claim of higher
        # precision — 10 of this model's 29 endpoints report "unknown",
        # DeepSeek's own first-party endpoint and Fireworks among them. It may
        # well be fp4 like Relace. The field is unreported metadata, so it is not
        # a reason to prefer this endpoint; judge both on behavior.
        "provider": {"order": ["digitalocean"], "allow_fallbacks": False},
        "capabilities": {"images": False, "video": False, "pdf": False},
    },
    "glm-5.3-flash-zai": {
        "label": "GLM 5.3 Flash (Z.AI fp8)",
        "short_label": "GLM 5.3F",
        "model": "z-ai/glm-5.3-flash",
        # First non-DeepSeek endpoint to earn a compiled-in slot, and the first
        # to match Relace on the measurement that actually matters. Probed
        # 2026-08-27 against the real 22.8k-token RAILS_AGENT_PROMPT with the
        # real tool set, 12 turns:
        #
        #   * 12/12 correct tool choice, 0 invented tool names, 0 raw-template
        #     leaks, 12/12 well-formed args, 12/12 second hops. (One second hop
        #     returned empty on a first run and not on a re-run — a flake, not a
        #     pattern, but it is the only blemish in 24 observed.)
        #   * reasoning_content populated on all 12; streams as deltas.
        #   * ~7s/turn. That is the trade — Relace is 1.6s. It is a reasoning
        #     model and the time is thinking, not a sick endpoint, but a latency-
        #     sensitive box should still prefer Relace.
        #
        # Prompt caching is the reason to want this one: 22,208 of 22,250 input
        # tokens came back as cache reads, at $0.015/M cached vs $0.075/M fresh.
        # Per-replica as always, which is what the provider pin protects.
        #
        # Pinned to Z.AI's own fp8 endpoint at the operator's explicit choice.
        # Note what that means: this is a first-party CHINESE host, and the move
        # of deepseek-v4-flash to Fireworks was made on jurisdiction grounds.
        # US-hosted alternatives for the same weights exist at 2x the token price
        # (baseten/fp8 and cloudflare, both $0.15/$0.50, 100%/99.9% uptime) —
        # swap the `order` below in the overlay to move without a rebuild.
        "provider": {"order": ["z-ai/fp8"], "allow_fallbacks": False},
        # images: MEASURED, not read off the model card — a 256x256 PNG is
        # described correctly through the pin, via data-URL or bare base64.
        # Beware the probe trap: an 8x8 PNG is rejected with `400 code 1210
        # 图片输入格式/解析错误`, which reads exactly like "no vision path".
        # video: the model card claims it; untested here, so it stays off.
        "capabilities": {"images": True, "video": False, "pdf": False},
    },
}


def _overlay_path() -> Path:
    """Where the client overlay lives. Returned whether or not it exists."""
    explicit = os.getenv(OVERLAY_PATH_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return Path(DEFAULT_OVERLAY_PATH)


def _read_overlay() -> dict:
    """Read the overlay's ``models`` map. Fail-open to ``{}`` on any problem."""
    path = _overlay_path()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read OpenRouter model overlay %s: %s", path, e)
        return {}

    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        if models is not None:
            logger.warning("%s: 'models' is not an object; ignoring overlay", path)
        return {}
    return models


def _normalize(name: str, raw: dict) -> Optional[dict]:
    """Validate one entry and fill in its defaults, or None if unusable.

    The only hard requirement is ``model`` — the OpenRouter model id. An entry
    without one cannot build a client, and silently registering it would put a
    dead option in the dropdown, so it is dropped loudly instead.
    """
    if not isinstance(raw, dict):
        logger.warning("OpenRouter entry %r is not an object; skipping", name)
        return None

    model_id = raw.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        logger.warning(
            "OpenRouter entry %r has no 'model' (the OpenRouter model id); skipping", name
        )
        return None

    caps = raw.get("capabilities")
    capabilities = dict(_DEFAULT_CAPABILITIES)
    if isinstance(caps, dict):
        # Only the three keys the rest of the app knows how to act on, coerced to
        # bool so a JSON string can't make `if caps['images']` accidentally true.
        for key in _DEFAULT_CAPABILITIES:
            if key in caps:
                capabilities[key] = bool(caps[key])

    entry = {
        "model": model_id.strip(),
        "label": str(raw.get("label") or name),
        "short_label": str(raw.get("short_label") or raw.get("label") or name),
        "capabilities": capabilities,
        # Whether this entry is offered at all. Lets an operator park a config
        # block (keeping the pricing notes) without it reaching the dropdown.
        "enabled": raw.get("enabled", True) is not False,
        # OpenRouter's thinking normalization differs from DeepSeek's own API, so
        # which client to build stays per-entry rather than sniffed from the id.
        # Default True: every model worth pinning an endpoint for today is a
        # reasoning model, and the reasoning-aware client is a superset.
        "reasoning": raw.get("reasoning", True) is not False,
    }

    # Provider routing (pin an endpoint / quantization) and any other top-level
    # OpenRouter request field. Merged into one extra_body, entry-provided
    # extra_body first so `provider` stays authoritative for the common case.
    extra_body = {}
    raw_extra = raw.get("extra_body")
    if isinstance(raw_extra, dict):
        extra_body.update(raw_extra)
    provider = raw.get("provider")
    if isinstance(provider, dict):
        extra_body["provider"] = provider
    if extra_body:
        entry["extra_body"] = extra_body

    return entry


def openrouter_models() -> dict:
    """The merged, validated registry: ``{frontend_name: entry}``.

    Overlay entries win on a name collision, so a box can re-point or re-price a
    compiled-in entry without a rebuild. Read fresh each call — the overlay is a
    mounted file an operator edits live, and caching it would mean a container
    restart just to try a different provider endpoint.
    """
    merged: dict = {}
    for source in (_BASE_MODELS, _read_overlay()):
        for name, raw in source.items():
            if not isinstance(name, str) or not name.strip():
                continue
            entry = _normalize(name.strip(), raw)
            if entry is not None:
                merged[name.strip()] = entry
    return {name: e for name, e in merged.items() if e["enabled"]}


def get_openrouter_model(name: str) -> Optional[dict]:
    """The registry entry for ``name``, or None if it isn't an OpenRouter model."""
    return openrouter_models().get(name)


def is_openrouter_model(name: str) -> bool:
    """True if ``name`` is served through OpenRouter (so get_llm must route it)."""
    return name in openrouter_models()


def api_base() -> str:
    """The OpenAI-compatible base URL, overridable for a proxy or a test double."""
    return os.getenv(API_BASE_ENV) or DEFAULT_API_BASE
