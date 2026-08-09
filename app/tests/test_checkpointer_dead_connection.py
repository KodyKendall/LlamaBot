"""Regression for the fleet-wide checkpointer crash:

    psycopg.OperationalError: consuming input failed: server closed the
    connection unexpectedly

Seen on leo-nefe (0.6.0e, rails_plan_mode_agent) inside
`handle_question_response` -> app.astream -> Pregel `_checkpointer_put_after_previous`
-> AsyncPostgresSaver.aput_writes -> cursor.executemany.

Cause: our AsyncConnectionPool is built WITHOUT `check=`, which is psycopg_pool's
default. A pooled connection whose server-side backend died while the connection
sat idle in the pool (postgres restart, `pg_terminate_backend`, an OOM-killed
backend, a proxy reaping the socket) is handed straight to the caller and blows
up on first use. `max_idle`/`max_lifetime` do not close this window — they only
recycle connections the pool *knows* are old.

Nothing retries it:
  * psycopg_pool does not re-check or replace a connection it already handed out.
  * AsyncPostgresSaver.aput_writes is a bare `executemany` with no retry.
  * LangGraph's `retry_policy` covers NODE execution only; the checkpointer put
    runs in the Pregel loop's teardown, outside any retry, and aborts the stream.
  * our own resilience ladder (app/agents/leonardo/resilience.py) is the LLM call
    path and does not classify psycopg.OperationalError as transient anyway.

So one dead pooled connection kills the whole user turn.
"""
import os
import inspect
import uuid

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


DEAD_CONN_MSG = "server closed the connection unexpectedly"


def _checkpoint_db_uri():
    return (
        os.getenv("CHECKPOINTER_DB_URI")
        or os.getenv("LEONARDO_DB_URI")
        or os.getenv("AUTH_DB_URI")
        or os.getenv("DB_URI")
    )


async def _open_pool_or_skip(**kwargs):
    """Open a 1-connection pool against the checkpoint DB, or skip cleanly.

    Short connect timeout on purpose: a URI may be present in env yet point at a
    host this container can't reach (CI without Postgres). We want a fast skip,
    not a 30s hang ending in PoolTimeout.
    """
    uri = _checkpoint_db_uri()
    if not uri:
        pytest.skip("no checkpoint DB URI in env")

    pool = AsyncConnectionPool(
        uri, open=False, min_size=1, max_size=1, timeout=5,
        kwargs={"connect_timeout": 5}, **kwargs,
    )
    try:
        await pool.open(wait=True, timeout=5)
    except Exception as exc:
        await pool.close()
        pytest.skip(f"checkpoint DB not reachable: {exc}")
    return pool


async def _kill_pooled_backend(pool):
    """Terminate the server-side backend of the pool's connection.

    This is exactly what a postgres restart looks like to an idle pooled
    connection: the socket is dead, but the pool still believes it is good.
    """
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT pg_backend_pid()")).fetchone()
        pid = row[0]

    killer = await psycopg.AsyncConnection.connect(
        _checkpoint_db_uri(), connect_timeout=5
    )
    try:
        await killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
    finally:
        await killer.close()


def _writes_config():
    return {
        "configurable": {
            "thread_id": str(uuid.uuid4()),
            "checkpoint_ns": "",
            "checkpoint_id": str(uuid.uuid4()),
        }
    }


# --- 1. the reproduction -----------------------------------------------------

@pytest.mark.asyncio
async def test_terminated_backend_reproduces_production_operational_error():
    """A pool built the way main.py builds it today reproduces the exact crash.

    Pinned as documentation of the mechanism: with no `check=`, aput_writes on a
    server-killed connection raises psycopg.OperationalError with the production
    message. This test keeps passing after the fix — it constructs its own
    unchecked pool.
    """
    pool = await _open_pool_or_skip()
    try:
        await _kill_pooled_backend(pool)
        saver = AsyncPostgresSaver(pool)

        with pytest.raises(psycopg.OperationalError) as exc_info:
            await saver.aput_writes(
                _writes_config(), [("__error__", "x")], task_id="t1", task_path=""
            )

        assert DEAD_CONN_MSG in str(exc_info.value)
    finally:
        await pool.close()


# --- 2. the fix, as a failing test -------------------------------------------

@pytest.mark.asyncio
async def test_checked_pool_survives_terminated_backend():
    """With `check=`, the pool discards the dead connection and opens a fresh one.

    psycopg_pool runs the check at CHECKOUT, so the caller never sees the corpse
    and the write just succeeds — no retry logic needed anywhere above it.
    """
    pool = await _open_pool_or_skip(check=AsyncConnectionPool.check_connection)
    try:
        await _kill_pooled_backend(pool)
        saver = AsyncPostgresSaver(pool)

        # Must NOT raise.
        await saver.aput_writes(
            _writes_config(), [("__error__", "x")], task_id="t1", task_path=""
        )
    finally:
        await pool.close()


def test_app_checkpointer_pools_are_built_with_a_connection_check():
    """Static guard so this runs in CI without Postgres.

    Every AsyncConnectionPool/ConnectionPool we hand to a checkpointer must be
    constructed with `check=`. Without it a single postgres restart kills the
    next turn on every box in the fleet.
    """
    from app import main as main_module
    from app.websocket import request_handler as rh_module

    offenders = []
    for module, funcs in (
        (main_module, ["get_or_create_checkpointer", "get_or_create_async_checkpointer"]),
        (rh_module, ["get_or_create_checkpointer"]),
    ):
        for fname in funcs:
            obj = getattr(module, fname, None)
            if obj is None:
                obj = getattr(getattr(module, "RequestHandler", None), fname, None)
            assert obj is not None, f"{module.__name__}.{fname} not found"
            src = inspect.getsource(obj)
            if "ConnectionPool(" in src and "check=" not in src:
                offenders.append(f"{module.__name__}.{fname}")

    assert not offenders, (
        "checkpointer connection pools built without `check=` (dead pooled "
        f"connections will be handed to callers): {offenders}"
    )


# --- 3. why no retry fired ---------------------------------------------------

@pytest.mark.asyncio
async def test_aput_writes_does_not_retry_a_dead_connection():
    """Pins the 'no automatic retry' half of the incident.

    One failed write == exactly one connection checkout. If anything in the
    checkpointer retried, the pool would be asked for a second connection.
    """
    checkouts = []

    class FakeCursor:
        async def executemany(self, query, params):
            raise psycopg.OperationalError(
                "consuming input failed: " + DEAD_CONN_MSG
            )

    class _Ctx:
        def __init__(self, val=None):
            self._val = val

        async def __aenter__(self):
            return self._val

        async def __aexit__(self, *a):
            return False

    class FakeConn:
        def cursor(self, **kwargs):
            return _Ctx(FakeCursor())

        def transaction(self):
            return _Ctx()

    # Must really be an AsyncConnectionPool — langgraph's get_connection()
    # dispatches on isinstance. Never opened; connection() is fully overridden.
    class FakePool(AsyncConnectionPool):
        def __init__(self):
            super().__init__(
                "postgresql://unused/unused", open=False, min_size=0, max_size=1
            )

        def connection(self, timeout=None):
            checkouts.append(1)
            return _Ctx(FakeConn())

    saver = AsyncPostgresSaver.__new__(AsyncPostgresSaver)
    saver.conn = FakePool()
    saver.pipe = None
    saver.supports_pipeline = False
    saver.lock = __import__("asyncio").Lock()

    with pytest.raises(psycopg.OperationalError):
        await saver.aput_writes(
            _writes_config(), [("__error__", "x")], task_id="t1", task_path=""
        )

    assert len(checkouts) == 1, (
        "aput_writes retried unexpectedly; this test pins that it does NOT, "
        "which is why the production error surfaced straight to the user"
    )


def test_psycopg_operational_error_is_not_covered_by_the_llm_resilience_ladder():
    """The existing transient-retry ladder is the LLM call path only.

    Documented so nobody assumes `is_transient_error` already covers this — a DB
    connection drop is not classified transient, and even if it were, the
    checkpointer put is never routed through that ladder.
    """
    from app.agents.leonardo.resilience import (
        is_transient_error,
        transient_exception_types,
    )

    err = psycopg.OperationalError("consuming input failed: " + DEAD_CONN_MSG)
    assert not is_transient_error(err)
    assert psycopg.OperationalError not in transient_exception_types()
    # ...and it isn't caught by the builtin ConnectionError rung either.
    assert not issubclass(psycopg.OperationalError, ConnectionError)
