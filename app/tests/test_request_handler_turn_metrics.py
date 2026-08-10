"""Tests for the request handler's turn-metrics wiring.

The handler owns three things the middleware cannot see: the turn's wall clock,
the user-perceived time-to-first-token, and shipping the rollup. These pin that
the rollup goes out for EVERY turn (including failed ones) and that it can
never itself break a turn.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.lib.turn_metrics import TurnMetrics
from app.websocket.request_handler import RequestHandler


def _handler_with_mothership(mothership):
    app = MagicMock()
    app.state.mothership_client = mothership
    return RequestHandler(app)


def _mothership():
    m = MagicMock()
    m.report_turn_metrics = AsyncMock(return_value=None)
    return m


def test_rollup_carries_total_wall_clock_and_segments():
    mothership = _mothership()
    handler = _handler_with_mothership(mothership)

    turn = TurnMetrics()
    turn.record_model_call(duration_ms=4000, ttft_ms=1000, output_tokens=300)
    turn.record_tool_call(name="write_file", duration_ms=500)

    async def main():
        import time
        # started_at 6s ago -> total_ms ~6000
        handler._report_turn_metrics(turn, time.monotonic() - 6.0, {
            "thread_id": "t-1", "agent_name": "rails_agent", "llm_model": "deepseek-v4-flash",
        })
        # let the fire-and-forget task run
        await asyncio.sleep(0)

    asyncio.run(main())

    kwargs = mothership.report_turn_metrics.call_args.kwargs
    assert kwargs["thread_id"] == "t-1"
    assert kwargs["agent_mode"] == "rails_agent"
    metrics = kwargs["metrics"]
    assert metrics["model_ms"] == 4000
    assert metrics["tool_ms"] == 500
    assert metrics["total_ms"] == pytest.approx(6000, abs=200)
    # 6000 total - 4000 model - 500 tool = the graph/checkpointer/serialization slice
    assert metrics["overhead_ms"] == pytest.approx(1500, abs=200)


def test_rollup_is_sent_even_when_the_turn_recorded_no_tools():
    mothership = _mothership()
    handler = _handler_with_mothership(mothership)
    turn = TurnMetrics()
    turn.record_model_call(duration_ms=1000, output_tokens=10)

    async def main():
        import time
        handler._report_turn_metrics(turn, time.monotonic(), {"thread_id": "t"})
        await asyncio.sleep(0)

    asyncio.run(main())
    assert mothership.report_turn_metrics.await_count == 1
    assert "slowest_tool" not in mothership.report_turn_metrics.call_args.kwargs["metrics"]


def test_reporting_never_raises_when_mothership_is_absent():
    # Self-hosted boxes have no mothership client on app.state.
    app = MagicMock()
    app.state.mothership_client = None
    handler = RequestHandler(app)

    async def main():
        import time
        handler._report_turn_metrics(TurnMetrics(), time.monotonic(), {"thread_id": "t"})

    asyncio.run(main())  # must not raise


def test_reporting_never_raises_when_the_snapshot_blows_up():
    # Defensive: a telemetry bug must not become the user's error. The turn is
    # already finished when this runs.
    mothership = _mothership()
    handler = _handler_with_mothership(mothership)

    broken = MagicMock()
    broken.snapshot.side_effect = ValueError("bad recorder")

    async def main():
        import time
        handler._report_turn_metrics(broken, time.monotonic(), {"thread_id": "t"})

    asyncio.run(main())  # must not raise
    mothership.report_turn_metrics.assert_not_called()
