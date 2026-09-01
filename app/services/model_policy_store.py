"""Model policy pushed down from the mothership.

The 2026-08-31 retirement of ``muse-spark-1.2-contributor`` took 89% of fleet turns
down at once, and the only way to move a box off it was to SSH in and edit ``.env``.
This is the channel that makes it one authenticated call to the fleet instead.

Carries three keys, all optional:

    {"default_model": "...", "disabled_models": [...], "enabled_models": [...]}

Stored on disk rather than in memory so it survives the container restart that a
model change usually needs, and written atomically so a half-written file can never
be read.

**Blast radius is the whole point of the care here.** One bad value reaches every
box in a single lease interval, so every consumer of this treats it as untrusted:
:func:`load` never raises and never returns a partial dict, and
``model_policy.remote_policy`` drops individual malformed keys rather than the
whole payload. The invariant that outranks the remote operator's intent is that the
box still resolves to a model it can actually build.
"""
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Beside instance.json, which is the box's other mothership-written config.
DEFAULT_PATH = ".leonardo/model_policy.json"
PATH_ENV = "MODEL_POLICY_PATH"

_lock = threading.Lock()

#: Only these are honoured; anything else in the payload is ignored, so the
#: mothership adding a key cannot change behaviour on a box that predates it.
_ALLOWED_KEYS = ("default_model", "disabled_models", "enabled_models")


def path() -> Path:
    return Path(os.getenv(PATH_ENV) or DEFAULT_PATH)


def load() -> dict:
    """The stored policy, or ``{}``. Never raises.

    A missing file is the normal case (most boxes are not steered remotely), so it
    is not worth a log line. A CORRUPT file is worth one, but is still ``{}`` — a
    box must not lose chat because a push was interrupted.
    """
    try:
        with path().open() as f:
            payload = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Ignoring unreadable model policy at %s: %s", path(), e)
        return {}

    if not isinstance(payload, dict):
        logger.warning("Ignoring model policy at %s: expected an object", path())
        return {}
    return {k: v for k, v in payload.items() if k in _ALLOWED_KEYS}


def save(policy: dict) -> None:
    """Replace the stored policy atomically.

    Written to a temp file in the same directory and renamed, so a reader either
    sees the old policy or the new one — never a truncated file. A partially
    written policy on every box at once is exactly the failure this channel exists
    to prevent.
    """
    if not isinstance(policy, dict):
        raise ValueError("model policy must be an object")

    filtered = {k: v for k, v in policy.items() if k in _ALLOWED_KEYS}
    target = path()

    with _lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".model_policy-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(filtered, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    logger.info("Model policy updated from the mothership: %s", filtered)


def clear() -> None:
    """Remove the stored policy, returning the box to its own configuration."""
    with _lock:
        try:
            path().unlink()
        except FileNotFoundError:
            pass
