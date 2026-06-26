"""Schema-level regression for the periodic checkpoint cleanup SQL.

Guards SupportIncident #106's secondary bug: the orphan sweep referenced
`b.checkpoint_id`, a column that does not exist on `checkpoint_blobs`
(PK: thread_id, checkpoint_ns, channel, version), so every periodic run crashed
with `column b.checkpoint_id does not exist`.

A FakeConn unit test can't catch a column-existence error, so this executes the
real sweep against the live PostgresSaver schema. Skips cleanly if no checkpoint
DB is reachable (e.g. CI without Postgres).
"""
import os
import inspect

import pytest

from app.services import checkpoint_cleanup
from app.services.checkpoint_cleanup import cleanup_stale_thread_checkpoints


def test_blob_orphan_query_does_not_reference_nonexistent_column():
    """Static guard: the sweep must never join blobs on the missing checkpoint_id."""
    src = inspect.getsource(cleanup_stale_thread_checkpoints)
    assert "b.checkpoint_id" not in src, (
        "checkpoint_blobs has no checkpoint_id column; the orphan-blob query must "
        "key on (thread_id, checkpoint_ns)"
    )


def _checkpoint_db_uri():
    return (
        os.getenv("CHECKPOINTER_DB_URI")
        or os.getenv("LEONARDO_DB_URI")
        or os.getenv("AUTH_DB_URI")
        or os.getenv("DB_URI")
    )


@pytest.mark.asyncio
async def test_cleanup_runs_against_real_schema_without_error():
    uri = _checkpoint_db_uri()
    if not uri:
        pytest.skip("no checkpoint DB URI in env")

    from psycopg_pool import AsyncConnectionPool

    # Short connect timeout: a URI may be set in env (e.g. CI's DB_URI) yet point
    # at a host that isn't reachable from the test container. We want a clean,
    # fast skip in that case — not a 30s hang followed by a PoolTimeout failure.
    pool = AsyncConnectionPool(
        uri, open=False, min_size=0, max_size=1, timeout=5,
        kwargs={"connect_timeout": 5},
    )
    try:
        # Confirm we can reach the DB and that it actually has the PostgresSaver
        # tables; skip cleanly otherwise (unreachable host, or a URI that points
        # at a non-checkpoint database). The cleanup call below is deliberately
        # left OUTSIDE this guard so a real regression still fails loudly.
        try:
            await pool.open(wait=True, timeout=5)
            async with pool.connection() as conn:
                res = await conn.execute(
                    "SELECT to_regclass('public.checkpoint_blobs'), "
                    "to_regclass('public.checkpoints')"
                )
                blobs_tbl, ckpt_tbl = await res.fetchone()
        except Exception as exc:  # connection/pool failure → nothing to guard here
            pytest.skip(f"checkpoint DB not reachable: {exc}")

        if blobs_tbl is None or ckpt_tbl is None:
            pytest.skip("checkpoint tables not present in this DB")

        # The actual assertion: this used to raise ProgrammingError on b.checkpoint_id.
        await cleanup_stale_thread_checkpoints(pool, stale_minutes=30)
    finally:
        await pool.close()
