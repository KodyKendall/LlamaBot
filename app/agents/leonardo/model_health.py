"""Short-lived memory of models that answered "I no longer exist".

Meta retired ``muse-spark-1.2-contributor`` on 2026-08-31 while it carried 89% of
fleet turns. Every request to it returned a deterministic 404. Without this module
each turn re-discovers that 404, waits for it, announces a fallback and only then
answers — for as long as the retirement lasts, which is forever.

Recording the death lets the policy layer route around it BEFORE spending a call.

Deliberately in-process and TTL'd rather than persisted:

- **In-process** because it is a cache, not a decision. A restart re-learns it from
  one cheap 404, and nothing important is lost. Persisting it would mean a wrong
  entry could outlive the incident that caused it.
- **TTL'd** because a retirement can be reversed and a 404 can be the provider
  having a bad day. A death record that never expires turns a transient blip into
  a permanent capability loss, which is a worse failure than the one it prevents.

The policy layer treats this as advisory and stays fail-open: if every known model
is marked gone, it still returns a model rather than locking the box out of chat.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

#: How long a model stays marked gone. Long enough that a real retirement is not
#: re-probed on every turn, short enough that a reversal heals without a restart.
TTL_SECONDS = 15 * 60

_lock = threading.Lock()
_gone: dict[str, float] = {}


def _now() -> float:
    """Monotonic clock, isolated so tests can move time without sleeping."""
    return time.monotonic()


def mark_model_gone(model_name: str) -> None:
    """Remember that ``model_name`` reported itself retired."""
    if not model_name:
        return
    with _lock:
        first_time = model_name not in _gone
        _gone[model_name] = _now()
    if first_time:
        logger.warning(
            "Model %s reported as retired by its provider; routing around it for "
            "the next %ds. If this is the box default, set DEFAULT_LLM_MODEL.",
            model_name, TTL_SECONDS,
        )


def is_gone(model_name: str) -> bool:
    """True if ``model_name`` was recently seen to be retired."""
    with _lock:
        marked_at = _gone.get(model_name)
        if marked_at is None:
            return False
        if _now() - marked_at > TTL_SECONDS:
            del _gone[model_name]
            return False
        return True


def gone_models() -> set[str]:
    """Every model currently marked gone, expired entries dropped."""
    with _lock:
        names = list(_gone)
    return {name for name in names if is_gone(name)}


def reset() -> None:
    """Forget every death record. For tests, and for an operator-forced re-probe."""
    with _lock:
        _gone.clear()
