"""Model policy pushed down from the mothership.

The 2026-08-31 retirement of ``muse-spark-1.2-contributor`` took 89% of fleet turns
down at once, and the only way to move a box off it was to SSH in and edit ``.env``.
This is the channel that makes it one authenticated call to the fleet instead.

Carries six keys, all optional:

    {"default_model": "...", "disabled_models": [...], "enabled_models": [...],
     "roles": {"chat": [...], "vision": [...]},
     "models": {"<name>": {"model": "...", "api_base": "...", "api_key_env": "..."}},
     "instance_overrides": {<any of the above, scoped to this one box>}}

``models`` REGISTERS models rather than choosing among compiled-in ones, which is
what makes adding one a push instead of a release — the gap the 2026-08-31
incident left open, where the fleet could be routed around a dead model only if
some other model was already compiled in. Entries are validated by
:mod:`app.agents.leonardo.openrouter_models`.

``roles`` maps a role to an ORDERED fallback chain rather than a single name, so a
first choice the box cannot serve degrades to a worse answer instead of a dead
turn. ``instance_overrides`` is the same document scoped to this box; the
mothership serves it already narrowed, so consuming it is a merge, not a lookup.

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
#:
#: The flip side, and the reason this list is worth checking before blaming the
#: mothership: a key the box does not know is dropped HERE, before
#: ``model_policy.remote_policy`` ever validates it. From the far side that is
#: indistinguishable from a push that never arrived.
_ALLOWED_KEYS = (
    "default_model",
    "disabled_models",
    "enabled_models",
    "roles",
    # Model REGISTRY entries, not just a choice among compiled-in models (0.7.7).
    # This is the key that makes adding a model a push instead of a release; see
    # app.agents.leonardo.openrouter_models, which validates every entry (a
    # pushed entry is untrusted input like the rest of this document, and may
    # not address a first-party credential at a host of its choosing).
    "models",
    "instance_overrides",
)


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
    # `key_hosts` declares which endpoints a credential this box HOLDS may be
    # sent to. It is deliberately NOT an allowed key: a document that grants
    # itself trust is not a boundary, so that declaration lives only in the
    # host-mounted operator overlay. Dropped silently by the filter below;
    # logged here because a mothership that keeps sending it should find out.
    if "key_hosts" in payload:
        logger.warning(
            "Ignoring 'key_hosts' in the pushed model policy: a pushed document "
            "cannot declare where this box's credentials may be sent. That "
            "declaration belongs in the operator overlay (.leonardo/openrouter_models.json)."
        )
    return {k: v for k, v in payload.items() if k in _ALLOWED_KEYS}


#: The uid/gid the customer's git tree and Rails app belong to on every box.
_APP_UID = 1000
_APP_GID = 1000


def _hand_to_app_user(file_path: str) -> None:
    """Make ``file_path`` readable and committable by uid 1000, best-effort."""
    try:
        os.chmod(file_path, 0o644)
    except OSError as e:
        logger.warning("Could not chmod the model policy file: %s", e)

    if os.geteuid() != 0:
        return
    try:
        os.chown(file_path, _APP_UID, _APP_GID)
    except OSError as e:
        logger.warning("Could not chown the model policy file to uid 1000: %s", e)


def save(policy: dict) -> None:
    """Replace the stored policy atomically.

    Written to a temp file in the same directory and renamed, so a reader either
    sees the old policy or the new one — never a truncated file. A partially
    written policy on every box at once is exactly the failure this channel exists
    to prevent.

    The file lands inside the customer's git working tree, and this process runs
    as root while Leo's checkpoint commits run as uid 1000. mkstemp's default
    root-owned 0600 therefore made ``git add -A`` abort with "unable to index
    file", silently stopping every save-point on the box (AgentTask #255). So the
    file is handed to uid 1000 at 0644 — best-effort, because a policy push must
    never take a box down over file ownership.
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
            _hand_to_app_user(tmp)
            # os.replace overwrites the destination regardless of ITS mode, so a
            # box already carrying the bad root-owned 0600 file recovers on the
            # next push without anyone running chmod by hand.
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
