"""Per-turn channel for telling the user something mid-run, from deep in the graph.

The resilience ladder lives inside ``DynamicModelMiddleware`` — several layers
below the websocket — and until now it was completely silent. When the Muse Spark
endpoint accepted requests and then sent zero chunks (2026-08-26, rsb-dev), Leo
retried the same stalled endpoint five times over ~10 minutes while the browser
showed nothing but a spinning shimmer. The customer's report was "it runs for like
10 minutes with no changes", because that is genuinely all there was to see.

Installed per turn in a ``ContextVar`` exactly like :mod:`app.lib.turn_metrics`
and :mod:`app.lib.rails_error_watch`: the request handler calls
:func:`start_turn_notices` with the run's sink *before* ``astream``, and LangGraph's
node tasks inherit a copy of that context, so middleware running inside a node
resolves to the same channel.

Two send paths because the ladder has two:

* :meth:`TurnNotices.asend` — awaited from ``awrap_model_call`` (the websocket
  chat path). Ordered with respect to the rest of the stream.
* :meth:`TurnNotices.send` — fire-and-forget from ``wrap_model_call`` and the raw
  StateGraph nodes, which run in a worker thread with no loop of their own. The
  frame is scheduled back onto the turn's loop.

Everything here is best effort. A status notice is never worth failing the turn it
is describing, so nothing in this module raises.
"""

import asyncio
import logging
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)

# Set per turn by start_turn_notices(). Node tasks spawned by LangGraph inherit a
# copy of the context, so they see the same channel instance.
_current_notices: ContextVar[Optional["TurnNotices"]] = ContextVar(
    "llamabot_turn_notices", default=None
)


class TurnNotices:
    """A websocket-shaped sink plus the loop it belongs to, scoped to one turn.

    ``send_json`` is whatever the request handler was handed — a real
    ``WebSocket`` or, for a background run, a ``RunSink`` (which stamps
    ``thread_id`` and logs the frame for replay). Both are awaited the same way,
    so notices inherit thread routing and reattach-replay for free.
    """

    def __init__(self, send_json, loop=None):
        self._send_json = send_json
        self._loop = loop

    async def asend(self, frame: dict) -> bool:
        """Await delivery of one frame. Returns whether it went out."""
        try:
            await self._send_json(frame)
            return True
        except Exception as e:  # noqa: BLE001 - a notice must never fail a turn
            logger.debug("turn notice not delivered: %s", e)
            return False

    def send(self, frame: dict) -> bool:
        """Schedule one frame from a sync call path. Returns whether it was queued.

        Sync middleware and raw StateGraph nodes run in a worker thread, so there
        is no loop here to await on — the coroutine is handed back to the turn's
        own loop instead. Fire-and-forget on purpose: the point of the notice is
        that the model is *not* responding, and blocking the retry on the notice
        would be the wrong trade.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is loop:
                loop.create_task(self.asend(frame))
            else:
                asyncio.run_coroutine_threadsafe(self.asend(frame), loop)
            return True
        except Exception as e:  # noqa: BLE001 - a notice must never fail a turn
            logger.debug("turn notice not scheduled: %s", e)
            return False


def start_turn_notices(send_json) -> Optional[TurnNotices]:
    """Install the notice channel for this turn's async context.

    Called from the request handler alongside ``start_turn``/``start_error_watch``
    and for the same reason. Returns the channel (or ``None`` if there is no loop
    to schedule onto, which only happens outside a server).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    notices = TurnNotices(send_json, loop=loop)
    _current_notices.set(notices)
    return notices


def clear_turn_notices() -> None:
    """Drop the channel for this context (the socket it points at is going away)."""
    _current_notices.set(None)


def current_notices() -> Optional[TurnNotices]:
    """The notice channel for this turn, or ``None`` when nothing is listening."""
    return _current_notices.get()


async def anotify(frame: dict) -> bool:
    """Await one notice, or do nothing if this turn has no channel."""
    notices = current_notices()
    if notices is None:
        return False
    return await notices.asend(frame)


def notify(frame: dict) -> bool:
    """Queue one notice from a sync path, or do nothing if there is no channel."""
    notices = current_notices()
    if notices is None:
        return False
    return notices.send(frame)


def thinking_frame(text: str) -> dict:
    """A status line for the thinking shimmer, with no model call behind it.

    Same shape ``/compact`` already pushes (``request_handler``): an
    ``AIMessageChunk`` carrying only a thinking block, so the browser renders it
    in the shimmer rather than as a message from Leo in the transcript.
    """
    return {
        "type": "AIMessageChunk",
        "content": "",
        "thinking": [{"type": "thinking", "thinking": text}],
    }
