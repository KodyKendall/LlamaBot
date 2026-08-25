"""Who a mothership report is about.

Every outbound report (``report_message``, ``report_error``,
``report_turn_metrics``, ``submit_feedback``, the overlay-ad fetch) used to
carry ``instance_name`` and nothing else about *who* was at the keyboard. On a
multi-user box that made the whole telemetry series unattributable: the
mothership could see that "instance foo sent 400 messages" but not that three
different people sent them.

This module builds the single wire shape those reports attach, and holds the
per-turn stamp the reporting paths read when the caller has no ``User`` object
in hand (the WebSocket turn is the big one — ``report_message`` is fired deep
inside the request handler, which is constructed before authentication).

**Mapping to a llamapress.ai account happens on the mothership, not here.**
``llamapress_user_guid`` is minted BY the mothership during unified login and
stored on the local shadow user (see app/routers/unified_login.py), so the box
is not translating anything — it is echoing back a foreign key the mothership
already owns. Legacy username/password users have no guid; they report with
``llamapress_user_guid: None`` and are only identifiable per-box, which is
correct: no llamapress.ai account exists to map them to.

``email`` rides along as a human-readable label for triage. It is a *synced
copy* of the mothership profile, never a join key — matching accounts by email
is exactly the spoofable path unified_login refuses to take.
"""

import logging
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)

#: The identity of the turn currently being served, stamped by the WebSocket
#: handler right after auth and read back by MothershipClient. A ContextVar (not
#: a global) because several turns run concurrently in one process; contextvars
#: are per-task and are copied into the ``asyncio.create_task`` fire-and-forget
#: reports the handler spawns.
#:
#: Fails closed, for the same reason app/lib/request_context.py does: unset
#: means "unknown", never "the only user on this box". Mis-attributed telemetry
#: is worse than absent telemetry.
_current: ContextVar[Optional[dict]] = ContextVar("llamabot_report_user", default=None)


def describe(user) -> Optional[dict]:
    """The wire shape for a ``User`` row. Never raises."""
    if user is None:
        return None
    try:
        return {
            "id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "email": getattr(user, "email", None),
            "llamapress_user_guid": getattr(user, "llamapress_user_guid", None),
            "role": getattr(user, "role", None),
            "is_admin": bool(getattr(user, "is_admin", False)),
        }
    except Exception as e:  # noqa: BLE001 - telemetry must never break a turn
        logger.info(f"Could not describe user for reporting: {e}")
        return None


def for_user_id(user_id) -> Optional[dict]:
    """Look the user up by local id and describe them. Never raises.

    Resolved WITHOUT a FastAPI dependency (and swallowing every failure) on
    purpose: a DB hiccup must degrade telemetry to "unattributed", not break
    the WebSocket connection this is called from.
    """
    if not isinstance(user_id, int):
        return None
    try:
        from sqlmodel import Session

        from app.db import engine
        from app.models import User

        if engine is None:
            return None
        with Session(engine) as session:
            return describe(session.get(User, user_id))
    except Exception as e:  # noqa: BLE001
        logger.info(f"Could not resolve user {user_id} for reporting: {e}")
        return None


def from_token_payload(payload: Optional[dict]) -> Optional[dict]:
    """Best-effort identity from a verified WS token, for callers with no DB row.

    Two cases reach this: a browser JWT whose user lookup failed (DB hiccup),
    and a ``llama_bot_rails`` gem token, which names a RAILS user in a different
    namespace and has no LlamaBot account behind it at all. Both are worth
    reporting as partial identities — "the Rails-embedded chat, Rails user 5"
    beats an unattributed row — but neither carries a guid, so neither maps to a
    llamapress.ai account.
    """
    if not isinstance(payload, dict):
        return None
    described = {
        "id": payload.get("user_id"),
        "username": payload.get("sub"),
        "email": None,
        "llamapress_user_guid": None,
        "role": payload.get("role"),
        "is_admin": bool(payload.get("is_admin", False)),
    }
    if payload.get("source"):
        described["source"] = payload["source"]
    if payload.get("rails_user_id") is not None:
        described["rails_user_id"] = payload["rails_user_id"]
    return described


def for_request(request) -> Optional[dict]:
    """Identity behind an HTTP request's session cookie, or None. Never raises."""
    try:
        from sqlmodel import Session

        from app.db import engine
        from app.dependencies import _user_from_session_cookie

        if engine is None:
            return None
        with Session(engine) as session:
            return describe(_user_from_session_cookie(request, session))
    except Exception as e:  # noqa: BLE001
        logger.info(f"Could not resolve request user for reporting: {e}")
        return None


def set_current(user_context: Optional[dict]) -> object:
    """Stamp the identity this turn belongs to. Returns a token for ``reset``."""
    return _current.set(user_context)


def current() -> Optional[dict]:
    """The identity this turn belongs to, or None if unknown."""
    return _current.get()


def reset(token: object) -> None:
    """Restore the previous value (best-effort; never raises)."""
    try:
        _current.reset(token)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        pass
