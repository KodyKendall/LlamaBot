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
  1a. **Model-switching lock** — when ``MODEL_SWITCHING_ALLOWED`` is off (the
     default), only the default text model is enabled, plus the vision model when
     ``VISION_MODEL_ALLOWED`` is on (so the image auto-switch still works). This
     coarse operator gate sits above the fail-open/allow-list logic below but
     still yields to an explicit disable in step 1.
  2. **Fail-open defaults** — ``deepseek-v4-flash`` (project default text model)
     and ``gpt-5-nano`` (the image auto-switch target) are globally
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

# --- Coarse operator gates (env-only, no user write path) --------------------
#
# Two admin toggles that sit ABOVE the per-model allow/disable lists:
#
#   * ``MODEL_SWITCHING_ALLOWED`` — when off, the instance is pinned to the
#     default text model (users can't pick another; the frontend hides the model
#     dropdown). The vision model stays reachable for the image auto-switch ONLY
#     when ``VISION_MODEL_ALLOWED`` is also on.
#   * ``VISION_MODEL_ALLOWED`` — when off, image/video attachments are refused
#     (frontend blocks the send with a support message; the backend also strips
#     them in ``_build_message_content`` as the authoritative gate).
#
# Both are read from the environment only — the box operator controls them, the
# instance user has no write path, exactly like ENABLED_MODELS/DISABLED_MODELS.
# Flip these two defaults to change fleet-wide behavior for instances that never
# set the vars.
_MODEL_SWITCHING_ALLOWED_DEFAULT = False
_VISION_ALLOWED_DEFAULT = False

# The single vision model the frontend image auto-switch targets. Kept reachable
# (when vision is allowed) even while manual switching is locked, so image sends
# still work without opening up the whole dropdown.
VISION_MODEL = "gpt-5-nano"

# Always enabled regardless of any allow-list, so every instance keeps a working
# text model (DeepSeek, the project default) and a working vision model
# (GPT-5 Nano — also the frontend image auto-switch target). These can
# still be turned off, but ONLY via an explicit disable override (see step 1
# above); an allow-list that omits them does not disable them.
_FAIL_OPEN_MODELS = frozenset({"deepseek-v4-flash", "gpt-5-nano"})

# Known frontend model names in preference order. Used only to choose a concrete
# fallback when the requested model is disabled; an allow-list may legitimately
# name models outside this set.
_KNOWN_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-gmi",
    "deepseek-v4-flash-fireworks",
    "claude-4.5-sonnet",
    "claude-4.5-haiku",
    "gpt-5-codex",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5.4-nano",
    "gpt-5.6-luna",
    # Same two models on the signed-in user's ChatGPT plan (see llm_factory's
    # _CHATGPT_SUBSCRIPTION_MODELS). Listed AFTER the API-key entries so
    # enabled_default_model() never picks a model that needs a user credential.
    "gpt-5.6-luna-chatgpt",
    "gpt-5.6-sol-chatgpt",
    "gemini-3-flash",
    "gemini-3-pro",
    "gemini-3.1-flash-lite",
    "qwen3.7-plus",
    "muse-spark-1.2-contributor",
]


def _csv_names(raw: str) -> list:
    """Split a comma-separated env value into a clean list of model names."""
    return [m.strip() for m in raw.split(",") if m.strip()]


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean operator gate from the environment.

    Unset or blank falls back to ``default``. Anything in the truthy set is True;
    everything else (including an explicit ``false``/``0``/``no``) is False.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def model_switching_allowed() -> bool:
    """True if the instance user may pick a model other than the default."""
    return _env_bool("MODEL_SWITCHING_ALLOWED", _MODEL_SWITCHING_ALLOWED_DEFAULT)


def vision_allowed() -> bool:
    """True if image/video attachments may be sent to a vision model."""
    return _env_bool("VISION_MODEL_ALLOWED", _VISION_ALLOWED_DEFAULT)


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
    # 2. Manual model-switching lock. When switching is off the instance is pinned
    #    to the default text model; the vision model stays reachable only when
    #    vision is also enabled, so the image auto-switch path keeps working. This
    #    sits above the allow-list/fail-open logic — it is the coarse operator
    #    gate — but still below an explicit disable in step 1.
    if not model_switching_allowed():
        if model_name == DEFAULT_LLM_MODEL:
            return True
        if model_name == VISION_MODEL and vision_allowed():
            return True
        return False
    # 3. The fail-open defaults are globally enabled (survive any allow-list).
    if model_name in _FAIL_OPEN_MODELS:
        return True
    # 4. An allow-list, if configured, restricts everything else.
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
