"""
Checkpoint Cleanup Service for LangGraph PostgresSaver

=== DELTACHANNEL CHANGED THE RULES (2026-06-16) ===

Agents whose state uses a `DeltaChannel`-backed `messages` field (see
`app/agents/utils/delta_state.py` — currently the whole RailsAgentState fleet
and the llamabot agent) store only an incremental delta per checkpoint plus a
periodic snapshot. Reconstructing the latest state REPLAYS the ancestor writes
back to the nearest snapshot. That means deleting a thread's intermediate
`checkpoint_writes` / `checkpoint_blobs` — which the old aggressive
`cleanup_thread_checkpoints_except_latest` did — would destroy the delta chain
and make the thread unreconstructable.

So cleanup is now SPLIT by agent type, gated on `graph_uses_delta_channel`:

1. DELTA agents: NO per-thread partial cleanup. DeltaChannel keeps per-checkpoint
   storage tiny (~O(N) instead of O(N^2)), so we simply retain full history. This
   also restores "continue" / time-travel for free.

2. NON-DELTA agents (legacy add_messages, e.g. the llamapress supervisor): keep
   the old POST-RUN trim (`cleanup_thread_checkpoints_except_latest`), which is
   safe because every checkpoint is a full snapshot. The caller in
   request_handler.py gates this on `graph_uses_delta_channel(app)`.

3. PERIODIC sweep (`cleanup_stale_thread_checkpoints`): now ORPHAN-ONLY. It only
   deletes `checkpoint_blobs` / `checkpoint_writes` rows with no surviving parent
   in `checkpoints` (debris from crashed/cancelled runs). Orphaned writes can
   never be part of a live delta chain, so this is safe for every agent. It no
   longer trims intermediate checkpoints, so it can run across the mixed fleet
   without a per-thread agent lookup.

=== TIME TRAVEL ===
With DeltaChannel retaining full per-thread history cheaply, time travel
(graph.get_state_history / graph.update_state) now works out of the box for
delta agents without the storage penalty that motivated the old aggressive trim.
===
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


def graph_uses_delta_channel(app, channel: str = "messages") -> bool:
    """Return True if the compiled graph stores `channel` in a DeltaChannel.

    Callers MUST skip destructive per-thread cleanup when this is True: a
    DeltaChannel reconstructs its value by replaying ancestor writes back to the
    nearest snapshot, so deleting intermediate writes/blobs corrupts the thread.

    Fails safe: if we can't introspect the graph, assume delta (skip destructive
    cleanup) rather than risk corrupting a delta thread.
    """
    try:
        from langgraph.channels.delta import DeltaChannel
        channels = getattr(app, "channels", None)
        if not channels or channel not in channels:
            # Can't positively confirm a non-delta channel — fail safe.
            return True
        return isinstance(channels[channel], DeltaChannel)
    except Exception as e:
        logger.warning(
            f"Could not determine DeltaChannel usage for graph ({e}); "
            f"assuming delta and skipping destructive cleanup to be safe."
        )
        return True


async def cleanup_thread_checkpoints_except_latest(pool, thread_id: str):
    """
    Trim all but the latest checkpoint for a SINGLE thread after a successful run.

    SAFETY: This is only safe for NON-DeltaChannel agents, where every checkpoint
    is a self-contained full snapshot. For DeltaChannel agents it would destroy
    the delta chain (snapshot blob + ancestor writes) and make the thread
    unreconstructable. The caller (request_handler.py) MUST gate this on
    `not graph_uses_delta_channel(app)`.

    Args:
        pool: AsyncConnectionPool for database access
        thread_id: The specific thread to clean up
    """
    async with pool.connection() as conn:
        # Delete all but latest checkpoint_blobs for this thread
        await conn.execute("""
            DELETE FROM checkpoint_blobs
            WHERE thread_id = %s
            AND checkpoint_id NOT IN (
                SELECT MAX(checkpoint_id) FROM checkpoints WHERE thread_id = %s
            )
        """, (thread_id, thread_id))

        # Delete all but latest checkpoint_writes for this thread
        await conn.execute("""
            DELETE FROM checkpoint_writes
            WHERE thread_id = %s
            AND checkpoint_id NOT IN (
                SELECT MAX(checkpoint_id) FROM checkpoints WHERE thread_id = %s
            )
        """, (thread_id, thread_id))

        # Delete all but latest checkpoints for this thread
        result = await conn.execute("""
            DELETE FROM checkpoints
            WHERE thread_id = %s
            AND checkpoint_id NOT IN (
                SELECT MAX(checkpoint_id) FROM checkpoints WHERE thread_id = %s
            )
            RETURNING checkpoint_id
        """, (thread_id, thread_id))

        deleted = len(await result.fetchall())
        if deleted > 0:
            logger.debug(f"Cleaned up {deleted} intermediate checkpoints for thread {thread_id[:8]}...")


async def cleanup_stale_thread_checkpoints(pool, stale_minutes: int = 30):
    """
    PERIODIC CLEANUP: collect orphaned checkpoint debris. DeltaChannel-safe.

    We no longer trim intermediate checkpoints here — DeltaChannel makes
    per-thread history cheap (and trimming it would corrupt delta chains). This
    only deletes `checkpoint_blobs` / `checkpoint_writes` rows whose
    (thread_id, checkpoint_id) has no surviving parent in `checkpoints`. Such
    orphans are debris from crashed/cancelled runs (a write landed but the
    checkpoint row never committed, or a parent was removed by the non-delta
    post-run trim); they can never be part of a live delta chain, so removing
    them is always safe for every agent type.

    Args:
        pool: AsyncConnectionPool for database access
        stale_minutes: accepted for signature/backwards compatibility. Orphan
            collection is always safe, so it no longer gates deletion.
    """
    async with pool.connection() as conn:
        # Count orphaned rows before cleanup (for logging)
        result = await conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM checkpoint_blobs b
                 WHERE NOT EXISTS (
                     SELECT 1 FROM checkpoints c
                     WHERE c.thread_id = b.thread_id AND c.checkpoint_id = b.checkpoint_id
                 )) AS orphan_blobs,
                (SELECT COUNT(*) FROM checkpoint_writes w
                 WHERE NOT EXISTS (
                     SELECT 1 FROM checkpoints c
                     WHERE c.thread_id = w.thread_id AND c.checkpoint_id = w.checkpoint_id
                 )) AS orphan_writes
        """)
        orphan_blobs, orphan_writes = await result.fetchone()

        # Delete orphaned blobs (the BIG storage consumers)
        await conn.execute("""
            DELETE FROM checkpoint_blobs b
            WHERE NOT EXISTS (
                SELECT 1 FROM checkpoints c
                WHERE c.thread_id = b.thread_id AND c.checkpoint_id = b.checkpoint_id
            )
        """)

        # Delete orphaned writes
        await conn.execute("""
            DELETE FROM checkpoint_writes w
            WHERE NOT EXISTS (
                SELECT 1 FROM checkpoints c
                WHERE c.thread_id = w.thread_id AND c.checkpoint_id = w.checkpoint_id
            )
        """)

        if orphan_blobs or orphan_writes:
            logger.info(
                f"Periodic checkpoint cleanup: removed {orphan_blobs} orphaned blob(s) "
                f"and {orphan_writes} orphaned write(s)"
            )


async def periodic_cleanup(pool, interval_hours: int = 24, stale_minutes: int = 30):
    """
    Background task that periodically collects orphaned checkpoint debris.

    Most storage is now bounded by DeltaChannel itself (delta agents) and the
    gated post-run trim (non-delta agents). This catch-all only removes orphaned
    writes/blobs left by crashed/cancelled runs.

    Args:
        pool: AsyncConnectionPool for database access
        interval_hours: How often to run cleanup (default 24h)
        stale_minutes: accepted for backwards compatibility (no longer gates
            deletion — see cleanup_stale_thread_checkpoints).
    """
    await asyncio.sleep(300)  # 5 min delay after startup

    while True:
        try:
            await cleanup_stale_thread_checkpoints(pool, stale_minutes)
        except Exception as e:
            logger.error(f"Periodic checkpoint cleanup error: {e}")

        await asyncio.sleep(interval_hours * 3600)
