"""Idempotency guard for inbound WebSocket chat messages.

A dropped WebSocket connection can cause the browser to re-send a chat message
it had already delivered (see the resend-on-reconnect logic in
``app/frontend/chat/index.js``). Without a guard the backend treats that re-send
as a brand-new turn: it cancels the in-flight run, appends a duplicate
HumanMessage to the thread, and restarts the agent from scratch — producing the
"Let me start by reading..." restart loop reported in 0.5.3a.

This registry enforces the invariant:

    A user message may be retried, but it may not be processed twice.

Keyed by ``(thread_id, client_message_id)``. Messages without a
``client_message_id`` (older browser builds, the Rails gem) are always treated
as new — they opt out of the guard rather than being blocked, so legacy clients
keep working unchanged.

This is the Layer 1 "seatbelt": an in-memory guard shared across connections via
the app-level :class:`WebSocketConnectionManager`, so a reconnect (which creates
a fresh handler) still sees messages registered by the previous connection. It
is intentionally process-local and not durable across restarts — durable replay
is Layer 2 (see docs/dev/websocket_background_runs.md).
"""
from collections import OrderedDict
import logging

logger = logging.getLogger(__name__)


class MessageDeduplicator:
    """Tracks recently-seen ``(thread_id, client_message_id)`` pairs (LRU-bounded)."""

    def __init__(self, max_entries: int = 2048):
        self._max_entries = max_entries
        # Insertion-ordered; oldest evicted first once we exceed max_entries.
        self._seen: "OrderedDict[tuple, bool]" = OrderedDict()

    def register(self, thread_id, client_message_id) -> bool:
        """Record a message and report whether it is new.

        Returns ``True`` if this ``(thread_id, client_message_id)`` has not been
        seen before — the caller should process it. Returns ``False`` if it is a
        duplicate re-send — the caller should ignore it (do NOT cancel the
        in-flight run or append the message again).

        Messages with a falsy ``client_message_id`` are always reported new and
        are never recorded, so clients that don't send the key are unaffected.
        """
        if not client_message_id:
            return True

        key = (str(thread_id), str(client_message_id))
        if key in self._seen:
            # Refresh recency so a long-lived hot key isn't evicted mid-use.
            self._seen.move_to_end(key)
            logger.info(
                f"Duplicate WebSocket message ignored (thread={thread_id}, "
                f"client_message_id={client_message_id})"
            )
            return False

        self._seen[key] = True
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return True

    def seen(self, thread_id, client_message_id) -> bool:
        """Read-only check (no recording). Used by tests/introspection."""
        if not client_message_id:
            return False
        return (str(thread_id), str(client_message_id)) in self._seen
