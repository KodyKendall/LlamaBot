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


# ---------------------------------------------------------------------------
# A wedged thread has to be visible to the USER, not only to telemetry
# ---------------------------------------------------------------------------

class TestWedgedThreadWarning:
    """The 2026-08-13 incident ran 18 minutes and 22 compactions and told the
    customer nothing. There is no in-chat recovery from a thread that has
    outgrown its context — the only escape is starting a new one, and we never
    said so.
    """

    def _websocket(self, open_=True):
        ws = MagicMock()
        ws.send_json = AsyncMock(return_value=None)
        ws.client_state = MagicMock()
        return ws

    def _handler(self, ws_open=True):
        handler = _handler_with_mothership(_mothership())
        handler._is_websocket_open = lambda ws: ws_open
        return handler

    def _wedged_turn(self):
        from app.lib.turn_metrics import COMPACTIONS_BEFORE_USER_WARNING

        turn = TurnMetrics(thread_id="t1", agent_mode="rails_agent")
        for _ in range(COMPACTIONS_BEFORE_USER_WARNING):
            turn.record_compaction()
        return turn

    def test_the_user_is_told_when_a_turn_spent_itself_compacting(self):
        handler, ws = self._handler(), self._websocket()
        asyncio.run(handler._warn_if_thread_is_wedged(self._wedged_turn(), ws))

        ws.send_json.assert_called_once()
        frame = ws.send_json.call_args[0][0]
        assert frame["type"] == "system_message"
        assert "new chat" in frame["content"].lower()
        # Frames must carry their thread so the client renders them in the right one.
        assert frame["thread_id"] == "t1"

    def test_an_ordinary_turn_says_nothing(self):
        handler, ws = self._handler(), self._websocket()
        turn = TurnMetrics(thread_id="t1")
        turn.record_compaction()  # one compaction is normal work on a long turn

        asyncio.run(handler._warn_if_thread_is_wedged(turn, ws))
        ws.send_json.assert_not_called()

    def test_nothing_is_sent_on_a_closed_socket(self):
        handler, ws = self._handler(ws_open=False), self._websocket()
        asyncio.run(handler._warn_if_thread_is_wedged(self._wedged_turn(), ws))
        ws.send_json.assert_not_called()

    def test_a_failure_to_warn_never_breaks_the_turn(self):
        handler, ws = self._handler(), self._websocket()
        ws.send_json = AsyncMock(side_effect=RuntimeError("socket died"))
        asyncio.run(handler._warn_if_thread_is_wedged(self._wedged_turn(), ws))

    def test_no_turn_recorder_is_harmless(self):
        handler, ws = self._handler(), self._websocket()
        asyncio.run(handler._warn_if_thread_is_wedged(None, ws))
        ws.send_json.assert_not_called()
