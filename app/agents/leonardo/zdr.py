"""Zero-data-retention (ZDR) mode, enforced inside LlamaBot (0.7.11).

A box flagged ZDR must never send customer data to any vendor except the models
it is allowed to use. Some of these boxes are under FERPA or a signed zero-training
DPA, so a leak here is a contract breach, not a bug.

The mothership already locks a ZDR box from the outside (model policy, ``.env``
keys, tracing flags). This module is the second, independent layer: the runtime
enforces ZDR itself, so one mistake on either side is not enough to leak.

**Where the flag comes from.** ``instance_overrides.zdr`` in the pushed model
policy (``.leonardo/model_policy.json``)::

    {"enabled": true, "allowed_models": ["deepseek-v4.1-flash-fireworks"],
     "sensitivity": "ferpa"}

Per-instance only. A ``zdr`` block in the fleet-level document is ignored.

**Two keys to open a model.** A model is usable under ZDR only if the mothership
lists it in ``allowed_models`` AND this build marks it ZDR-compliant
(:data:`app.agents.leonardo.model_capabilities.ZDR_COMPLIANT`). So adding a ZDR
model takes a mothership change plus a LlamaBot release. That is on purpose: a
wrong name in a pushed document cannot, on its own, open a retaining vendor.

**Fail closed.** Once a box has seen ``zdr.enabled: true`` it writes a lock file
next to the policy (``zdr_lock.json``). The lock is removed only when the
mothership pushes a policy that no longer carries the flag (or clears the policy
outright). If the policy file is missing or unreadable while the lock exists,
ZDR stays on with NO permitted models, so every model call is refused rather than
falling back to a default vendor. A mothership outage at startup therefore
cannot turn ZDR off.
"""

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)

LOCK_FILE_NAME = "zdr_lock.json"

#: What the agent/user sees when a turn is refused. Plain on purpose.
REFUSAL_MESSAGE = (
    "This workspace is in zero-data-retention mode and has no approved model "
    "available right now. Please contact support."
)
WEB_SEARCH_DISABLED_MESSAGE = "Web search is disabled on this workspace."

_TRACING_ENV_VARS = ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING")


class ZDRRefused(RuntimeError):
    """A call that would send data somewhere a ZDR box may not send it."""

    def __init__(self, message: str = REFUSAL_MESSAGE):
        super().__init__(message)


@dataclass(frozen=True)
class ZDRState:
    enabled: bool
    allowed_models: tuple = ()
    sensitivity: str = ""
    # True when ZDR is on only because of the lock file (no usable policy on disk).
    locked: bool = False


OFF = ZDRState(enabled=False)


# ---------------------------------------------------------------------------
# Reading the flag
# ---------------------------------------------------------------------------

def clean_zdr(value) -> Optional[dict]:
    """Validate an ``instance_overrides.zdr`` block, or None if unusable."""
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
        logger.warning("Remote model policy: ignoring malformed zdr %r", value)
        return None
    allowed = value.get("allowed_models", [])
    if not isinstance(allowed, list) or not all(isinstance(m, str) for m in allowed):
        logger.warning("Remote model policy: ignoring malformed zdr.allowed_models %r", allowed)
        return None
    sensitivity = value.get("sensitivity", "")
    return {
        "enabled": value["enabled"],
        "allowed_models": [m.strip() for m in allowed if m.strip()],
        "sensitivity": sensitivity if isinstance(sensitivity, str) else "",
    }


def requested_by(policy: dict) -> bool:
    """True if a policy document ASKS for ZDR, even if the rest of the block is malformed.

    Used when saving, so a half-valid ZDR block still arms the lock (and then
    refuses every call) instead of silently leaving the box open.
    """
    override = policy.get("instance_overrides") if isinstance(policy, dict) else None
    if not isinstance(override, dict):
        return False
    block = override.get("zdr")
    return isinstance(block, dict) and block.get("enabled") is True


def _store():
    from app.services import model_policy_store

    return model_policy_store


def lock_path():
    return _store().path().with_name(LOCK_FILE_NAME)


def write_lock(sensitivity: str = "") -> None:
    """Remember that this box is ZDR. Best-effort, never raises."""
    try:
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabled": True, "sensitivity": sensitivity}))
        _store()._hand_to_app_user(str(path))
    except Exception as e:  # noqa: BLE001
        logger.error("Could not write the ZDR lock file: %s", e)


def clear_lock() -> None:
    try:
        lock_path().unlink()
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.error("Could not remove the ZDR lock file: %s", e)


def _lock_sensitivity() -> Optional[str]:
    """The lock's sensitivity label, or None when there is no lock."""
    path = lock_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        label = data.get("sensitivity", "") if isinstance(data, dict) else ""
        return label if isinstance(label, str) else ""
    except Exception:  # noqa: BLE001 — an unreadable lock is still a lock
        return ""


def zdr_state() -> ZDRState:
    """This box's ZDR state right now. Never raises."""
    try:
        raw = _store().load()
        override = raw.get("instance_overrides")
        block = clean_zdr(override.get("zdr")) if isinstance(override, dict) else None
        if block and block["enabled"]:
            if _lock_sensitivity() is None:
                write_lock(block["sensitivity"])
            state = ZDRState(True, tuple(block["allowed_models"]), block["sensitivity"])
        else:
            sensitivity = _lock_sensitivity()
            if sensitivity is not None:
                state = ZDRState(True, (), sensitivity, locked=True)
            else:
                state = OFF
    except Exception as e:  # noqa: BLE001 — fail closed on anything unexpected
        logger.error("Could not read ZDR state (%s); refusing model calls.", e)
        state = ZDRState(True, (), "", locked=True)
    _note_state(state)
    return state


def enforced() -> bool:
    return zdr_state().enabled


# ---------------------------------------------------------------------------
# Which models a ZDR box may use
# ---------------------------------------------------------------------------

def is_zdr_compliant(model_name: str) -> bool:
    """LlamaBot's own verdict on a model, independent of the mothership.

    The customer's own ChatGPT plan and every config-registered gateway entry are
    hard False: config cannot vouch for a vendor's retention terms, and a
    registry entry can shadow a compiled name and point it at another host.
    """
    from app.agents.leonardo.llm_factory import _CHATGPT_SUBSCRIPTION_MODELS
    from app.agents.leonardo.model_capabilities import ZDR_COMPLIANT
    from app.agents.leonardo.openrouter_models import is_openrouter_model

    if model_name in _CHATGPT_SUBSCRIPTION_MODELS:
        return False
    if is_openrouter_model(model_name):
        return False
    return ZDR_COMPLIANT.get(model_name, False) is True


def compliant_models() -> list:
    from app.agents.leonardo.model_capabilities import ZDR_COMPLIANT

    return sorted(m for m in ZDR_COMPLIANT if is_zdr_compliant(m))


def permitted_models(state: Optional[ZDRState] = None) -> list:
    """``allowed_models ∩ compliant``, in the mothership's order. Empty = refuse."""
    state = state or zdr_state()
    seen = []
    for name in state.allowed_models:
        if name not in seen and is_zdr_compliant(name):
            seen.append(name)
    return seen


def check_model(model_name: str) -> None:
    """Raise :class:`ZDRRefused` if this box is ZDR and may not use ``model_name``."""
    state = zdr_state()
    if state.enabled and model_name not in permitted_models(state):
        logger.error("ZDR: refusing to build model %r (permitted: %s)",
                     model_name, permitted_models(state))
        raise ZDRRefused()


def refuse_if_enforced(what: str) -> None:
    """For code paths that talk to a fixed vendor and cannot be rerouted."""
    if enforced():
        logger.error("ZDR: refusing %s (it calls a vendor directly)", what)
        raise ZDRRefused()


class ZDRBlockedChatModel(BaseChatModel):
    """Stand-in model for when ZDR leaves nothing to build. Every call refuses."""

    @property
    def _llm_type(self) -> str:
        return "zdr-blocked"

    def _generate(self, *args, **kwargs):
        raise ZDRRefused()

    async def _agenerate(self, *args, **kwargs):
        raise ZDRRefused()


# ---------------------------------------------------------------------------
# Tracing, logging and reporting
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_last_state: Optional[ZDRState] = None
_saved_tracing_env: Optional[dict] = None


def _set_tracing(enabled_by_zdr: bool) -> None:
    """Force LangSmith off in-process under ZDR, whatever the env says.

    ``langsmith.configure(enabled=False)`` sets the global fallback that
    ``tracing_is_enabled`` reads before the env, and langsmith caches env reads,
    so the env is overwritten AND the cache cleared too. Leaving ZDR restores both.
    """
    global _saved_tracing_env
    try:
        import langsmith
        from langsmith import utils as ls_utils

        if enabled_by_zdr:
            if _saved_tracing_env is None:
                _saved_tracing_env = {v: os.environ.get(v) for v in _TRACING_ENV_VARS}
            for var in _TRACING_ENV_VARS:
                os.environ[var] = "false"
            langsmith.configure(enabled=False)
        elif _saved_tracing_env is not None:
            for var, value in _saved_tracing_env.items():
                if value is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = value
            _saved_tracing_env = None
            langsmith.configure(enabled=None)
        if hasattr(ls_utils.get_env_var, "cache_clear"):
            ls_utils.get_env_var.cache_clear()
    except Exception as e:  # noqa: BLE001
        logger.error("ZDR: could not change LangSmith tracing: %s", e)


def tracing_enabled() -> bool:
    try:
        from langchain_core.tracers.context import _tracing_v2_is_enabled

        return bool(_tracing_v2_is_enabled())
    except Exception:  # noqa: BLE001
        return False


def _note_state(state: ZDRState) -> None:
    """Apply side effects and log once per state change."""
    global _last_state
    with _state_lock:
        if state == _last_state:
            return
        _last_state = state
    _set_tracing(state.enabled)
    if state.enabled:
        allowed = permitted_models(state)
        logger.warning(
            "ZDR enforced: allowed_models=%s sensitivity=%s%s",
            allowed, state.sensitivity or "none",
            " (no usable policy on disk; refusing model calls)" if state.locked else "",
        )
    else:
        logger.info("ZDR off")


def report() -> dict:
    """The box's own ZDR attestation, sent with ``report_health``."""
    state = zdr_state()
    return {
        "enforced": state.enabled,
        "allowed_models": permitted_models(state) if state.enabled else [],
        "compliant_models": compliant_models(),
        "tracing": "on" if tracing_enabled() else "off",
        "web_search": "off" if state.enabled else "on",
    }


def _reset_for_tests() -> None:
    global _last_state, _saved_tracing_env
    _last_state = None
    _saved_tracing_env = None
