"""The raw StateGraph agents must contribute model timing too.

rails_beginner_agent, rails_ai_builder_agent and rails_plain_chat_mode invoke
the model directly, so TurnMetricsMiddleware never sees them. Without this,
beginner mode — a mode real customers live in — would report total_ms and
ttft_ms but zero model_ms, making its overhead look enormous and its provider
look free. Both readings would be wrong.

``invoke_with_transient_retry`` is the one call site all three share, so timing
goes there.
"""
import asyncio

import pytest

from app.agents.leonardo.resilience import invoke_with_transient_retry
from app.lib.turn_metrics import start_turn


class _Msg:
    def __init__(self, usage_metadata=None):
        self.usage_metadata = usage_metadata
        self.response_metadata = {"model_name": "deepseek-v4-flash"}


def test_raw_node_invocation_is_recorded_as_a_model_call():
    async def main():
        turn = start_turn(agent_mode="rails_beginner_agent")
        invoke_with_transient_retry(
            lambda: _Msg({"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280}),
            label="rails_beginner_agent/deepseek-v4-flash",
        )
        return turn.snapshot()

    snap = asyncio.run(main())
    assert snap["model_calls"] == 1
    assert snap["output_tokens"] == 80
    assert snap["input_tokens"] == 1200
    assert snap["model_ms"] >= 0


def test_retries_are_recorded_as_one_model_call_with_the_total_wait():
    # A turn that silently retried twice took 3x as long as the provider's
    # nominal latency. Reporting three separate "fast" calls would hide the
    # wait the user actually sat through.
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("connection reset by peer")
        return _Msg({"input_tokens": 5, "output_tokens": 5, "total_tokens": 10})

    async def main():
        turn = start_turn()
        invoke_with_transient_retry(flaky, label="test")
        return turn.snapshot()

    snap = asyncio.run(main())
    assert calls["n"] == 3
    assert snap["model_calls"] == 1


def test_a_failed_raw_invocation_is_still_timed():
    async def main():
        turn = start_turn()
        with pytest.raises(ValueError):
            invoke_with_transient_retry(
                lambda: (_ for _ in ()).throw(ValueError("deterministic 400")),
                label="test",
            )
        return turn.snapshot()

    assert asyncio.run(main())["model_calls"] == 1


def test_no_turn_active_is_a_noop():
    # Headless executor and scheduled jobs run these agents with no recorder.
    sentinel = _Msg()
    assert invoke_with_transient_retry(lambda: sentinel, label="test") is sentinel
