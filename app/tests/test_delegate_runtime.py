"""Regression coverage for cancellable, observable sub-agent delegation."""

import asyncio

import pytest

from app.agents.leonardo.delegation import DelegationTimedOut, run_delegation


class _BlockingAgent:
    def __init__(self):
        self.cancelled = False

    async def ainvoke(self, input_data, config=None):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _SuccessfulAgent:
    async def ainvoke(self, input_data, config=None):
        return {"messages": ["done"]}


@pytest.mark.asyncio
async def test_delegation_timeout_cancels_nested_agent_and_emits_terminal_progress():
    agent = _BlockingAgent()
    events = []

    with pytest.raises(DelegationTimedOut, match="timed out after 0.02s"):
        await run_delegation(
            agent,
            {"messages": [{"role": "user", "content": "research"}]},
            timeout_seconds=0.02,
            heartbeat_seconds=0.005,
            stream_writer=events.append,
            label="Research sub-agent",
        )

    assert agent.cancelled is True
    assert events[0]["phase"] == "started"
    assert any(event["phase"] == "working" for event in events)
    assert events[-1]["phase"] == "timed_out"
    assert all(event["type"] == "delegation_progress" for event in events)


@pytest.mark.asyncio
async def test_delegation_success_preserves_result_and_emits_completed():
    events = []

    result = await run_delegation(
        _SuccessfulAgent(),
        {"messages": [{"role": "user", "content": "research"}]},
        timeout_seconds=1,
        heartbeat_seconds=0.01,
        stream_writer=events.append,
        label="Research sub-agent",
        config={"metadata": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["done"]}
    assert [event["phase"] for event in events] == ["started", "completed"]


@pytest.mark.asyncio
async def test_delegation_failure_emits_failed_and_reraises():
    class _FailingAgent:
        async def ainvoke(self, input_data, config=None):
            raise RuntimeError("provider exploded")

    events = []
    with pytest.raises(RuntimeError, match="provider exploded"):
        await run_delegation(
            _FailingAgent(),
            {"messages": []},
            timeout_seconds=1,
            stream_writer=events.append,
        )

    assert events[-1]["phase"] == "failed"
