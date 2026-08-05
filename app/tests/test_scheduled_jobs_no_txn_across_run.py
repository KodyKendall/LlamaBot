"""Regression test for SupportIncident #137 — the scheduled-jobs `/invoke` connection leak.

`invoke_due_jobs` read the due-jobs list on the request-scoped session (opening an
implicit transaction) and then held it open across `await execute_agent_headless(...)`.
The connection sat `idle in transaction` for the whole agent run; if that run hung, the
connection was pinned forever. One leak per due-poll (cron hits /invoke every minute)
compounded until llamabot's pool was dry — at which point the LangGraph Postgres
checkpointer blocked forever and every chat turn went silently dead (crm box, ~40h).

The invariant under test: **no pool connection is checked out while an agent run is
awaited.** Asserted with pool checkout/checkin events, so it holds on any dialect.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine, select

from app.models import ScheduledJob, SchedulerInvocationLog


@pytest.fixture
def sqlite_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'sched.db'}", echo=False)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def checkout_counter(sqlite_engine):
    """Live count of connections checked out of the pool."""
    state = {"n": 0, "peak": 0}

    @event.listens_for(sqlite_engine, "checkout")
    def _on_checkout(*_args):
        state["n"] += 1
        state["peak"] = max(state["peak"], state["n"])

    @event.listens_for(sqlite_engine, "checkin")
    def _on_checkin(*_args):
        state["n"] -= 1

    return state


@pytest.fixture
def due_job(sqlite_engine):
    job = ScheduledJob(
        name="Daily Conversations Summary",
        description="repro of the crm box job",
        agent_name="rails_agent",
        prompt="summarize",
        llm_model="deepseek-v4-flash",
        cron_expression="0 8 * * *",
        timezone="UTC",
        max_duration_seconds=300,
        recursion_limit=100,
        is_enabled=True,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    with Session(sqlite_engine) as s:
        s.add(job)
        s.commit()
        s.refresh(job)
        return job.id


def _fake_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        app=SimpleNamespace(state=SimpleNamespace()),
    )


@pytest.mark.asyncio
async def test_no_connection_held_while_agent_runs(
    monkeypatch, sqlite_engine, checkout_counter, due_job
):
    from app.routers import scheduled_jobs as sj
    from app.services import headless_agent_executor

    monkeypatch.setattr(sj, "engine", sqlite_engine, raising=False)

    observed = []

    async def fake_execute(**kwargs):
        # This is the moment that used to pin an `idle in transaction` connection.
        observed.append(checkout_counter["n"])
        return SimpleNamespace(id=99, status="completed")

    monkeypatch.setattr(headless_agent_executor, "execute_agent_headless", fake_execute)

    result = await sj.invoke_due_jobs(request=_fake_request(), user=None)

    assert result["jobs_executed"] == 1
    assert observed, "the executor was never called — the job was not picked up as due"
    assert observed == [0], (
        f"a pool connection was checked out during the agent run (saw {observed}); "
        "the due-jobs read transaction is being held across the await"
    )


@pytest.mark.asyncio
async def test_pool_is_drained_after_invoke(
    monkeypatch, sqlite_engine, checkout_counter, due_job
):
    """Every session opened by /invoke must be returned to the pool."""
    from app.routers import scheduled_jobs as sj
    from app.services import headless_agent_executor

    monkeypatch.setattr(sj, "engine", sqlite_engine, raising=False)

    async def fake_execute(**kwargs):
        return SimpleNamespace(id=1, status="completed")

    monkeypatch.setattr(headless_agent_executor, "execute_agent_headless", fake_execute)

    await sj.invoke_due_jobs(request=_fake_request(), user=None)

    assert checkout_counter["n"] == 0, "invoke leaked a checked-out connection"


@pytest.mark.asyncio
async def test_job_timing_and_invocation_log_are_still_persisted(
    monkeypatch, sqlite_engine, checkout_counter, due_job
):
    """Moving the writes into short-lived sessions must not lose them."""
    from app.routers import scheduled_jobs as sj
    from app.services import headless_agent_executor

    monkeypatch.setattr(sj, "engine", sqlite_engine, raising=False)

    async def fake_execute(**kwargs):
        return SimpleNamespace(id=7, status="completed")

    monkeypatch.setattr(headless_agent_executor, "execute_agent_headless", fake_execute)

    await sj.invoke_due_jobs(request=_fake_request(), user=None)

    with Session(sqlite_engine) as s:
        job = s.get(ScheduledJob, due_job)
        assert job.last_run_at is not None, "last_run_at was not written"
        assert job.next_run_at > datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=5
        ), "next_run_at was not advanced"

        logs = s.exec(select(SchedulerInvocationLog)).all()
        assert len(logs) == 1
        assert logs[0].status == "success"
        assert logs[0].jobs_checked == 1
        assert logs[0].jobs_executed == 1


@pytest.mark.asyncio
async def test_hanging_agent_does_not_pin_a_connection(
    monkeypatch, sqlite_engine, checkout_counter, due_job
):
    """The compounding-deadlock case: a stalled run must not hold the pool."""
    import asyncio

    from app.routers import scheduled_jobs as sj
    from app.services import headless_agent_executor

    monkeypatch.setattr(sj, "engine", sqlite_engine, raising=False)

    started = asyncio.Event()
    release = asyncio.Event()

    async def hanging_execute(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(id=1, status="completed")

    monkeypatch.setattr(headless_agent_executor, "execute_agent_headless", hanging_execute)

    task = asyncio.create_task(sj.invoke_due_jobs(request=_fake_request(), user=None))
    await asyncio.wait_for(started.wait(), timeout=5)

    # The agent is mid-run and stuck. Nothing may be checked out of the pool.
    assert checkout_counter["n"] == 0, (
        "a hung agent run is pinning a pool connection — this is the fleet outage"
    )

    release.set()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_no_jobs_due_still_logs_and_releases(monkeypatch, sqlite_engine, checkout_counter):
    from app.routers import scheduled_jobs as sj

    monkeypatch.setattr(sj, "engine", sqlite_engine, raising=False)

    result = await sj.invoke_due_jobs(request=_fake_request(), user=None)

    assert result["jobs_executed"] == 0
    assert checkout_counter["n"] == 0
    with Session(sqlite_engine) as s:
        logs = s.exec(select(SchedulerInvocationLog)).all()
        assert len(logs) == 1
        assert logs[0].status == "no_jobs_due"
