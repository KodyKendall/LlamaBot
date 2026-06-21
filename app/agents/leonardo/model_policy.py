"""Operator/mothership gate on which LLM models an instance may use.

This is distinct from the per-model API-key check (a model with no key is merely
*unconfigured*). Here a model can be fully present — key set, `get_llm` branch
exists — yet deliberately **disabled**, so the instance user cannot select it and
cannot re-enable it. The policy is sourced only from operator-controlled channels
the user has no write path to:

  * The mothership-provisioned instance config, ``.leonardo/instance.json``, keys
    ``enabled_models`` and ``disabled_models`` (JSON arrays of frontend model
    names). Authoritative: mothership owns this file.
  * The ``ENABLED_MODELS`` / ``DISABLED_MODELS`` environment variables
    (comma-separated), baked into the container by the box operator.

**Resolution (most-specific wins):**

  1. **Explicit disable** — a model named in ``disabled_models`` /
     ``DISABLED_MODELS`` is OFF, overriding everything below (including the
     fail-open defaults). This is the deliberate "turn off even a default" knob.
     Disable sources UNION: any source can turn a model off.
  2. **Fail-open defaults** — ``deepseek-v4-flash`` (project default text model)
     and ``gemini-3.1-flash-lite`` (the image auto-switch target) are globally
     enabled, so every instance always keeps a working text *and* vision model,
     even when an allow-list is configured. Only an explicit disable (step 1)
     turns them off.
  3. **Allow-list** — if ``enabled_models`` / ``ENABLED_MODELS`` is configured,
     only the named models are enabled (for everything not covered above). Allow
     sources INTERSECT: neither can broaden what the other restricts.
  4. **Inert** — if no allow-list is configured, every model is enabled (still
     gated by its API key, exactly as before).

**Where it is enforced.** The real chokepoint is :func:`get_llm` (in
``llm_factory``): a disabled requested model is replaced with an enabled one before
a client is built. The ``/api/available-models`` endpoint reflects the same rule so
the dropdown greys out disabled models — but that is UX only. Because the websocket
``llm_model`` field is unvalidated user input, gating the dropdown alone would be
bypassable; get_llm is the authoritative gate.
"""

import json
import logging
import os
from typing import Optional

from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

logger = logging.getLogger(__name__)

_INSTANCE_CONFIG_PATH = ".leonardo/instance.json"

# Always enabled regardless of any allow-list, so every instance keeps a working
# text model (DeepSeek, the project default) and a working vision model
# (Gemini 3.1 Flash Lite — also the frontend image auto-switch target). These can
# still be turned off, but ONLY via an explicit disable override (see step 1
# above); an allow-list that omits them does not disable them.
_FAIL_OPEN_MODELS = frozenset({"deepseek-v4-flash", "gemini-3.1-flash-lite"})

# Known frontend model names in preference order. Used only to choose a concrete
# fallback when the requested model is disabled; an allow-list may legitimately
# name models outside this set.
_KNOWN_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "claude-4.5-sonnet",
    "claude-4.5-haiku",
    "gpt-5-codex",
    "gpt-5-mini",
    "gemini-3-flash",
    "gemini-3-pro",
    "gemini-3.1-flash-lite",
    "qwen3.7-plus",
]


def _csv_names(raw: str) -> list:
    """Split a comma-separated env value into a clean list of model names."""
    return [m.strip() for m in raw.split(",") if m.strip()]


def _read_instance_config() -> Optional[dict]:
    """Load instance.json, or None if absent/unreadable. Patched in tests."""
    try:
        with open(_INSTANCE_CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read %s for model policy: %s", _INSTANCE_CONFIG_PATH, e)
        return None


def _instance_list(key: str) -> Optional[list]:
    """Return instance.json[key] as a clean list, or None if absent/empty."""
    config = _read_instance_config()
    if not config:
        return None
    value = config.get(key)
    if isinstance(value, list):
        names = [str(m).strip() for m in value if str(m).strip()]
        return names or None
    return None


def _allowlist() -> Optional[set]:
    """Effective allow-list as a set, or None when no allow-list is configured.

    Intersect each configured source so neither can broaden what the other
    restricts; a source that configures nothing does not constrain.
    """
    env_allow = _csv_names(os.environ.get("ENABLED_MODELS", "")) or None
    allow: Optional[set] = None
    for source in (_instance_list("enabled_models"), env_allow):
        if source is None:
            continue
        source_set = set(source)
        allow = source_set if allow is None else (allow & source_set)
    return allow


def _disabled_set() -> set:
    """Models explicitly disabled. Union across sources — any source can disable."""
    disabled: set = set()
    env_disabled = _csv_names(os.environ.get("DISABLED_MODELS", ""))
    instance_disabled = _instance_list("disabled_models") or []
    disabled.update(env_disabled)
    disabled.update(instance_disabled)
    return disabled


def is_model_enabled(model_name: str) -> bool:
    """True if ``model_name`` is permitted by the operator/mothership policy.

    See the module docstring for the full resolution order.
    """
    # 1. Explicit disable wins over everything — even the fail-open defaults.
    if model_name in _disabled_set():
        return False
    # 2. The fail-open defaults are globally enabled (survive any allow-list).
    if model_name in _FAIL_OPEN_MODELS:
        return True
    # 3. An allow-list, if configured, restricts everything else.
    allow = _allowlist()
    if allow is None:
        return True
    return model_name in allow


def enabled_default_model() -> str:
    """A concrete enabled model to fall back to. Never raises, never empty.

    Prefers the project default; otherwise the first enabled known model; if the
    policy somehow disables every model we know how to build (misconfiguration),
    returns the project default anyway so the instance is never locked out of chat.
    """
    if is_model_enabled(DEFAULT_LLM_MODEL):
        return DEFAULT_LLM_MODEL
    for name in _KNOWN_MODELS:
        if is_model_enabled(name):
            return name
    logger.warning(
        "Model policy disables all known models; falling back to %s. "
        "Check enabled_models/disabled_models in instance.json and "
        "ENABLED_MODELS/DISABLED_MODELS.",
        DEFAULT_LLM_MODEL,
    )
    return DEFAULT_LLM_MODEL
