"""Who the current agent turn is being run for.

``get_llm(model_name)`` is a pure name -> client function called deep inside the
agent graph, with no reference to the websocket frame that started the turn. The
ChatGPT-subscription models need to know *whose* credential to spend, so the
websocket layer stamps the authenticated user id here at the start of each turn
and ``get_llm`` reads it back.

A ContextVar (not a global) because several turns can be in flight at once in the
same process; contextvars are per-task and are copied into tasks and
``asyncio.to_thread`` calls spawned from the turn.

Fails closed on purpose: when nothing is set, ``current_user_id()`` returns None
and the subscription models fall back to the operator's default model. It must
never guess "the only user on this box" — that is exactly how one user's
subscription would end up paying for another user's turn.
"""

from contextvars import ContextVar
from typing import Optional

_current_user_id: ContextVar[Optional[int]] = ContextVar(
    "llamabot_current_user_id", default=None
)


def set_current_user_id(user_id: Optional[int]) -> object:
    """Stamp the user this turn belongs to. Returns a token for ``reset``."""
    return _current_user_id.set(user_id)


def current_user_id() -> Optional[int]:
    """The user this turn belongs to, or None if unknown."""
    return _current_user_id.get()


def reset_current_user_id(token: object) -> None:
    """Restore the previous value (best-effort; never raises)."""
    try:
        _current_user_id.reset(token)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        pass
