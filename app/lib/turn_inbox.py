"""Per-turn inbox: messages the user sent while this turn was running.

A new chat message no longer cancels a running turn — RunManager queues it on
the thread (see app/websocket/run_manager.py). This module is how the agent
picks it up mid-turn: the request handler installs a drain function with
:func:`start_turn_inbox` before ``astream``, and ``QueuedMessagesMiddleware``
calls :func:`drain_turn_inbox` before every model call. LangGraph's node tasks
inherit a copy of the context, same as :mod:`app.lib.turn_notices`.

Delegated sub-agents inherit the context too, so ``run_delegation`` blanks the
inbox for their duration — the user's message belongs to the main agent.
"""

import logging
from contextvars import ContextVar
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# drain() -> list[BaseMessage]; set per turn, None outside a websocket turn.
_current_inbox: ContextVar[Optional[Callable[[], list]]] = ContextVar(
    "llamabot_turn_inbox", default=None
)


def start_turn_inbox(drain: Optional[Callable[[], list]]):
    """Install this turn's drain function. Returns the ContextVar token."""
    return _current_inbox.set(drain)


def suspend_turn_inbox():
    """Hide the inbox (for a nested sub-agent). Pass the token to :func:`restore_turn_inbox`."""
    return _current_inbox.set(None)


def restore_turn_inbox(token) -> None:
    _current_inbox.reset(token)


def drain_turn_inbox() -> list:
    """Messages queued since the last model call, as LangChain messages. Never raises."""
    drain = _current_inbox.get()
    if drain is None:
        return []
    try:
        return list(drain() or [])
    except Exception as e:  # noqa: BLE001 - a queued message must never fail the turn
        logger.warning("Could not drain queued messages: %s", e)
        return []
