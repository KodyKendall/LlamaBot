"""Remote "sleep lock" for a Leonardo instance.

The mothership owns the decision (a free instance is about to be put to sleep
and backed up). The instance mirrors that decision into a ``SiteSetting`` row so
it survives a container restart / ``bin/update``, and the browser learns about it
by polling ``GET /api/instance-lock`` — no page refresh needed.

Deliberately NOT in ``VALID_SITE_SETTINGS``: the instance owner is an admin on
their own box, and that PUT is engineer-or-admin, so exposing the key there would
let them unlock themselves. The only write paths are the mothership-authenticated
``POST /api/instance-lock`` and the LeaseManager poll.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session

logger = logging.getLogger(__name__)

#: SiteSetting key holding the JSON lock payload.
SETTING_KEY = "instance_lock"

#: Shown when the mothership doesn't override the copy.
DEFAULT_TITLE = "Your free Leo is about to sleep"
DEFAULT_BODY = (
    "We are backing up your Leo so you don't lose your work. "
    "Upgrade for a Leo that never sleeps."
)
DEFAULT_UPGRADE_URL = "https://llamapress.ai/pricing"

#: SiteSetting.value is max_length=1000 — keep the override copy well under it.
MAX_VALUE_LEN = 1000

UNLOCKED = {
    "locked": False,
    "title": DEFAULT_TITLE,
    "body": DEFAULT_BODY,
    "upgrade_url": DEFAULT_UPGRADE_URL,
}


def _coerce(payload: Optional[dict]) -> dict:
    """Normalize a stored/incoming payload into the shape the frontend expects."""
    payload = payload or {}
    return {
        "locked": bool(payload.get("locked")),
        "title": str(payload.get("title") or DEFAULT_TITLE),
        "body": str(payload.get("body") or DEFAULT_BODY),
        "upgrade_url": str(payload.get("upgrade_url") or DEFAULT_UPGRADE_URL),
    }


def get_lock_state(session: Session) -> dict:
    """Current lock state. Fails OPEN — a down auth DB must not lock everyone out."""
    from app.routers.api import get_site_setting

    raw = get_site_setting(session, SETTING_KEY, default="")
    if not raw:
        return dict(UNLOCKED)
    try:
        return _coerce(json.loads(raw))
    except (ValueError, TypeError) as e:
        logger.warning(f"Malformed {SETTING_KEY} value, treating as unlocked: {e}")
        return dict(UNLOCKED)


def set_lock_state(session: Session, payload: dict) -> dict:
    """Persist the lock state and return the normalized payload.

    Raises ``ValueError`` if the (optionally overridden) copy would exceed the
    SiteSetting column — better a loud 400 than a silently truncated modal.
    """
    from app.models import SiteSetting

    state = _coerce(payload)
    value = json.dumps(state)
    if len(value) > MAX_VALUE_LEN:
        raise ValueError("Lock payload too large (title/body/upgrade_url must fit in 1000 chars of JSON)")

    setting = session.get(SiteSetting, SETTING_KEY)
    if setting:
        setting.value = value
        setting.updated_at = datetime.now(timezone.utc)
    else:
        setting = SiteSetting(key=SETTING_KEY, value=value)
    session.add(setting)
    session.commit()
    logger.info(f"Instance lock set to locked={state['locked']}")
    return state


def is_locked(session: Session) -> bool:
    return bool(get_lock_state(session).get("locked"))
