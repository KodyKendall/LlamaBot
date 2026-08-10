"""Database side of the .env settings surface: custom variables and pending restarts.

Two concerns, both needing storage that survives a container recreate (which is
precisely the event these features are about):

  * **Custom variables** — rows in ``custom_env_var``. The database is the source
    of truth; the ``.env`` managed block is a rendered artifact. See
    :class:`app.models.CustomEnvVar`.
  * **Pending restarts** — which edits have been written to the file but are not
    yet live in a running process. Kept in a ``SiteSetting`` row so the list
    survives the very restart it is tracking, and self-clears afterwards.

**Pending entries store a fingerprint, never a value.** Recording the new value
would put freshly-rotated API keys in a second place, so each entry keeps a
short SHA-256 prefix instead. That is enough to answer the only question asked
of it — "is the running process's value the one we wrote?" — with no disclosure.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models import CustomEnvVar, SiteSetting
from app.services import env_settings_service as svc

logger = logging.getLogger(__name__)

#: Not in ``VALID_SITE_SETTINGS`` — internal bookkeeping, not a user toggle, and
#: it must not be settable through the generic site-settings PUT.
PENDING_KEY = "pending_env_changes"

#: ``SiteSetting.value`` is ``max_length=1000``; each entry costs ~60 bytes.
#: Well past this many pending changes the exact list stops mattering — the
#: instance needs a restart either way — so we keep the newest and say so.
MAX_PENDING = 15


def fingerprint(value: str) -> str:
    """Short, non-reversible stand-in for a value, used to detect "already live"."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Custom variables
# --------------------------------------------------------------------------

def custom_var_map(session: Session) -> dict:
    """``{name: value}`` for every custom variable — what the renderer consumes."""
    try:
        rows = session.exec(select(CustomEnvVar)).all()
    except Exception as e:
        logger.warning("Could not read custom env vars: %s", e)
        return {}
    return {row.name: row.value for row in rows}


def list_custom_vars(session: Session) -> list:
    """Custom variables for the Settings panel — names and metadata, never values.

    The value is replaced with a fixed mask rather than a derived hint. These are
    user-supplied but an instance can have several users, and one operator's
    service token is not another's to read.
    """
    try:
        rows = session.exec(select(CustomEnvVar)).all()
    except Exception as e:
        logger.warning("Could not list custom env vars: %s", e)
        return []
    return sorted(
        (
            {
                "name": row.name,
                "description": row.description,
                "masked": svc.MASK,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ),
        key=lambda entry: entry["name"],
    )


def upsert_custom_var(
    session: Session,
    name: str,
    value: str,
    description: Optional[str] = None,
    user_id: Optional[int] = None,
) -> dict:
    """Create or update a custom variable and re-render the managed block.

    Raises :class:`~app.services.env_settings_service.EnvValidationError` when the
    name or value is rejected, or when the name is reserved. The render step drops
    collisions regardless — this check exists so the user is told at save time
    instead of silently getting an inactive row.

    Every rejection uses the SAME message, whether the name is reserved by the
    platform or merely already present in this instance's file. A per-case message
    would turn this endpoint into an oracle for probing what is configured on the
    box, which is exactly what the rest of this surface refuses to disclose.
    """
    name = svc.validate_name(name)
    value = svc.validate_value(value)

    if svc.is_reserved(name):
        raise svc.EnvValidationError(svc.RESERVED_MESSAGE)

    now = datetime.now(timezone.utc)
    row = session.get(CustomEnvVar, name)
    if row:
        row.value = value
        if description is not None:
            row.description = description
        row.updated_at = now
    else:
        row = CustomEnvVar(
            name=name,
            value=value,
            description=description,
            created_by_user_id=user_id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    session.commit()

    svc.sync_managed_block(custom_var_map(session))
    # Rails reads the merged file at container start, so this is always pending.
    mark_pending(session, name, value)
    return {"name": name, "shadowed": False}


def delete_custom_var(session: Session, name: str) -> bool:
    """Remove a custom variable and re-render. Returns False if it wasn't there."""
    row = session.get(CustomEnvVar, name)
    if not row:
        return False
    session.delete(row)
    session.commit()
    svc.sync_managed_block(custom_var_map(session))
    mark_pending(session, name, "")
    return True


# --------------------------------------------------------------------------
# Pending restart tracking
# --------------------------------------------------------------------------

def _read_pending(session: Session) -> list:
    try:
        row = session.get(SiteSetting, PENDING_KEY)
    except Exception as e:
        logger.warning("Could not read pending env changes: %s", e)
        return []
    if not row or not row.value:
        return []
    try:
        data = json.loads(row.value)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _write_pending(session: Session, entries: list) -> None:
    value = json.dumps(entries[-MAX_PENDING:])
    row = session.get(SiteSetting, PENDING_KEY)
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = SiteSetting(key=PENDING_KEY, value=value)
        session.add(row)
    session.commit()


def mark_pending(session: Session, key: str, new_value: str, username: str = "") -> None:
    """Record that ``key`` was written to the file but isn't live yet."""
    entries = [e for e in _read_pending(session) if e.get("k") != key]
    entries.append({
        "k": key,
        "f": fingerprint(new_value),
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "u": username[:40],
    })
    try:
        _write_pending(session, entries)
    except Exception as e:
        # Bookkeeping must never fail the edit that already succeeded on disk.
        logger.warning("Could not record pending env change for %s: %s", key, e)


def reconcile_pending(session: Session) -> list:
    """Drop entries whose value is now live, and return what is still pending.

    Called at startup and whenever the list is read, so a restart clears the
    banner on its own — there is no "I restarted" button to fall out of sync with
    reality.
    """
    import os

    entries = _read_pending(session)
    if not entries:
        return []

    still_pending = [
        e for e in entries
        if fingerprint(os.environ.get(e.get("k", ""), "")) != e.get("f")
    ]

    if len(still_pending) != len(entries):
        try:
            _write_pending(session, still_pending)
        except Exception as e:
            logger.warning("Could not prune pending env changes: %s", e)

    return still_pending


def pending_summary(session: Session) -> dict:
    """``{"count": n, "keys": [...], "restart_required": bool}`` for the UI banner."""
    pending = reconcile_pending(session)
    return {
        "count": len(pending),
        "keys": [e.get("k") for e in pending],
        "restart_required": bool(pending),
    }
