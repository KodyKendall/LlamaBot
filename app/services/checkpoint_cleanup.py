"""
Checkpoint Cleanup Service for LangGraph PostgresSaver

=== CLEANUP STRATEGY ===

We use TWO complementary cleanup approaches:

1. POST-RUN CLEANUP (cleanup_thread_checkpoints_except_latest):
   - Called after each SUCCESSFUL run completes
   - Cleans up intermediate checkpoints for THAT SPECIFIC THREAD ONLY
   - Other users' threads are NOT affected
   - Keeps "continue" working during active errors (checkpoints still exist)
   - Immediate cleanup = no storage buildup

2. PERIODIC CLEANUP (cleanup_stale_thread_checkpoints):
   - Runs every 24 hours as a catch-all
   - ONLY cleans threads with no activity for 30+ minutes
   - Protects active users who might be recovering from errors
   - Uses ThreadMetadata.updated_at to determine staleness
   - Ensures storage stays bounded even if post-run cleanup fails

=== TIME TRAVEL CAPABILITY (DISABLED) ===
LangGraph supports "time travel" - the ability to rewind a conversation to any
previous checkpoint and branch from there. This is useful for:
- Debugging agent behavior at specific points
- "Undo" functionality for users
- A/B testing different conversation paths

To ENABLE time travel in the future:
1. Remove the post-run cleanup call in request_handler.py
2. Modify periodic cleanup to keep last N checkpoints per thread
3. Use graph.get_state_history(config) to list available checkpoints
4. Use graph.update_state(config, values, as_node) to rewind

Alternative cleanup (keeps last N checkpoints for time travel):
    DELETE FROM checkpoints
    WHERE (thread_id, checkpoint_id) NOT IN (
        SELECT thread_id, checkpoint_id FROM (
            SELECT thread_id, checkpoint_id,
                   ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY checkpoint_id DESC) as rn
            FROM checkpoints
        ) ranked WHERE rn <= 5  -- Keep last 5 checkpoints per thread
    )

For now, we don't use time travel, so we aggressively clean up to save storage.
===
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def cleanup_thread_checkpoints_except_latest(pool, thread_id: str):
    """
    Clean up intermediate checkpoints for a SINGLE thread after successful run.

    Call this after astream() completes successfully. This keeps:
    - "Continue" functionality (checkpoints exist during active run with errors)
    - Thread resumability (latest checkpoint preserved)

    While immediately cleaning up:
    - Tool call checkpoints
    - Node transition checkpoints
    - Any intermediate state

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
    PERIODIC CLEANUP: Delete old checkpoints only for STALE threads.

    A thread is "stale" if its most recent activity (from ThreadMetadata.updated_at)
    is older than stale_minutes. This protects active threads where users might
    be recovering from errors.

    Example: If Person A has an error and Person B finishes successfully,
    Person A's thread won't be cleaned up until 30+ minutes of inactivity.

    We use ThreadMetadata.updated_at because:
    - It's already tracked by the existing system
    - It's updated on every message (see thread_service.py)
    - It's a proper TIMESTAMP column (easy to query)

    Args:
        pool: AsyncConnectionPool for database access
        stale_minutes: Only clean threads with no activity for this long (default 30)
    """
    async with pool.connection() as conn:
        # Count before cleanup
        result = await conn.execute("SELECT COUNT(*) FROM checkpoints")
        before_count = (await result.fetchone())[0]

        # Find stale threads by joining with ThreadMetadata
        # Only clean threads that haven't had activity in stale_minutes
        await conn.execute("""
            WITH stale_threads AS (
                -- Find threads with no recent activity
                -- Uses ThreadMetadata.updated_at which is updated on every message
                SELECT thread_id
                FROM threadmetadata
                WHERE updated_at < NOW() - INTERVAL '%s minutes'
            )
            DELETE FROM checkpoints
            WHERE thread_id IN (SELECT thread_id FROM stale_threads)
            AND (thread_id, checkpoint_id) NOT IN (
                -- Keep the latest checkpoint for each stale thread
                SELECT thread_id, MAX(checkpoint_id)
                FROM checkpoints
                WHERE thread_id IN (SELECT thread_id FROM stale_threads)
                GROUP BY thread_id
            )
        """, (stale_minutes,))

        # Clean up orphaned blobs (the BIG storage consumers)
        await conn.execute("""
            DELETE FROM checkpoint_blobs
            WHERE (thread_id, checkpoint_id) NOT IN (
                SELECT thread_id, checkpoint_id FROM checkpoints
            )
        """)

        # Clean up orphaned writes
        await conn.execute("""
            DELETE FROM checkpoint_writes
            WHERE (thread_id, checkpoint_id) NOT IN (
                SELECT thread_id, checkpoint_id FROM checkpoints
            )
        """)

        # Count after cleanup
        result = await conn.execute("SELECT COUNT(*) FROM checkpoints")
        after_count = (await result.fetchone())[0]

        deleted = before_count - after_count
        if deleted > 0:
            logger.info(f"Periodic checkpoint cleanup: deleted {deleted} checkpoints from stale threads ({before_count} -> {after_count})")


async def periodic_cleanup(pool, interval_hours: int = 24, stale_minutes: int = 30):
    """
    Background task that runs cleanup periodically as a catch-all.

    Most cleanup happens via post-run cleanup, but this catches:
    - Failed/crashed runs
    - Threads that weren't properly cleaned
    - Any edge cases

    IMPORTANT: Only cleans up threads that have been inactive for stale_minutes.
    This protects users who are actively recovering from errors.

    Args:
        pool: AsyncConnectionPool for database access
        interval_hours: How often to run cleanup (default 24h)
        stale_minutes: Only clean threads inactive for this long (default 30 min)
    """
    await asyncio.sleep(300)  # 5 min delay after startup

    while True:
        try:
            await cleanup_stale_thread_checkpoints(pool, stale_minutes)
        except Exception as e:
            logger.error(f"Periodic checkpoint cleanup error: {e}")

        await asyncio.sleep(interval_hours * 3600)
