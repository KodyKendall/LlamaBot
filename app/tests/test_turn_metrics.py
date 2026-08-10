"""Tests for per-turn performance accounting (app/lib/turn_metrics.py).

These pin the arithmetic that answers the two questions we could not answer
before: "is the model actually generating slower?" and "where did the wall
clock of this turn actually go?". See docs/dev/performance_telemetry.md.

Pure unit tests — no event loop, no LangGraph. The recorder is deliberately
I/O-free so it can be tested without building a graph (building one clears the
asyncio loop in CI; see the note in app/tests/test_agents.py).
"""
import pytest

from app.lib.turn_metrics import TurnMetrics, current_turn, start_turn


def test_tokens_per_second_uses_decode_time_not_total_call_time():
    # A call that waited 2s for the first token then streamed 300 tokens over
    # 3s is generating at 100 tok/s. Dividing by the whole 5s (60 tok/s) would
    # blame the provider's decode rate for queueing/prefill latency.
    m = TurnMetrics()
    m.record_model_call(duration_ms=5000, ttft_ms=2000, output_tokens=300)
    assert m.snapshot()["tokens_per_second"] == pytest.approx(100.0)


def test_tokens_per_second_falls_back_to_full_duration_without_ttft():
    # Non-streaming calls report no TTFT; the whole call is the only estimate.
    m = TurnMetrics()
    m.record_model_call(duration_ms=4000, output_tokens=200)
    assert m.snapshot()["tokens_per_second"] == pytest.approx(50.0)


def test_tokens_per_second_aggregates_across_calls():
    # A turn is many model calls; the rate is total output over total decode
    # time, NOT the mean of per-call rates (which over-weights tiny calls).
    m = TurnMetrics()
    m.record_model_call(duration_ms=1000, ttft_ms=0, output_tokens=10)
    m.record_model_call(duration_ms=9000, ttft_ms=0, output_tokens=990)
    assert m.snapshot()["tokens_per_second"] == pytest.approx(100.0)


def test_tokens_per_second_is_none_when_no_decode_time():
    m = TurnMetrics()
    m.record_model_call(duration_ms=0, output_tokens=0)
    assert m.snapshot()["tokens_per_second"] is None


def test_overhead_is_wall_clock_not_spent_in_model_or_tools():
    # This is the number that tests the "LangGraph/checkpointer loop is the
    # bottleneck" theory: time the turn spent neither waiting on a provider
    # nor running a tool.
    m = TurnMetrics()
    m.record_model_call(duration_ms=4000, output_tokens=100)
    m.record_tool_call(name="write_file", duration_ms=1000)
    snap = m.snapshot(total_ms=6000)
    assert snap["model_ms"] == 4000
    assert snap["tool_ms"] == 1000
    assert snap["overhead_ms"] == 1000


def test_overhead_never_goes_negative_with_parallel_tool_calls():
    # Tools can run concurrently, so summed tool time legitimately exceeds the
    # turn's wall clock. A negative "overhead" would be nonsense on a chart.
    m = TurnMetrics()
    m.record_tool_call(name="a", duration_ms=3000)
    m.record_tool_call(name="b", duration_ms=3000)
    assert m.snapshot(total_ms=3200)["overhead_ms"] == 0


def test_ttft_is_first_token_of_the_whole_turn():
    # The user-perceived wait is until the FIRST token appears, which may be
    # several model calls in (a turn can open with a tool call).
    m = TurnMetrics()
    m.mark_first_token(elapsed_ms=1500)
    m.mark_first_token(elapsed_ms=9000)
    assert m.snapshot()["ttft_ms"] == 1500


def test_slowest_tool_is_surfaced_for_triage():
    m = TurnMetrics()
    m.record_tool_call(name="read_file", duration_ms=40)
    m.record_tool_call(name="browser_inspect", duration_ms=2100)
    snap = m.snapshot()
    assert snap["slowest_tool"] == {"name": "browser_inspect", "ms": 2100}


def test_input_tokens_tracks_the_largest_prompt_sent():
    # The long-thread hypothesis: prompt size grows every turn. The peak is
    # what matters, and summarization can shrink it mid-turn.
    m = TurnMetrics()
    m.record_model_call(duration_ms=10, output_tokens=1, input_tokens=48000)
    m.record_model_call(duration_ms=10, output_tokens=1, input_tokens=12000)
    assert m.snapshot()["input_tokens"] == 48000


def test_last_model_call_carries_per_message_timings():
    # Attached to the assistant report_message so each reply carries the
    # timing of the call that produced it.
    m = TurnMetrics()
    m.record_model_call(duration_ms=5000, ttft_ms=2000, output_tokens=300, model="deepseek-v4-flash")
    m.record_model_call(duration_ms=2000, ttft_ms=500, output_tokens=150, model="deepseek-v4-flash")
    last = m.last_model_call()
    assert last["duration_ms"] == 2000
    assert last["ttft_ms"] == 500
    assert last["tokens_per_second"] == pytest.approx(100.0)


def test_last_model_call_is_none_before_any_call():
    assert TurnMetrics().last_model_call() is None


def test_snapshot_counts_calls():
    m = TurnMetrics()
    m.record_model_call(duration_ms=1, output_tokens=1)
    m.record_model_call(duration_ms=1, output_tokens=1)
    m.record_tool_call(name="t", duration_ms=1)
    snap = m.snapshot()
    assert snap["model_calls"] == 2
    assert snap["tool_calls"] == 1


def test_recorder_is_reachable_from_nested_async_context():
    # The middleware runs inside LangGraph's node tasks, which are spawned from
    # the request handler's context. asyncio copies the context into each task,
    # so the ContextVar resolves to the SAME mutable recorder and the node's
    # measurements reach the handler that started the turn.
    import asyncio

    async def main():
        turn = start_turn(thread_id="t-1", agent_mode="rails_agent")

        async def node():
            current_turn().record_model_call(duration_ms=1234, output_tokens=7)

        await asyncio.create_task(node())
        return turn.snapshot()

    assert asyncio.run(main())["model_ms"] == 1234


def test_current_turn_is_none_outside_a_turn():
    # Instrumentation must be a no-op when nothing started a turn (background
    # jobs, headless executor, tests) rather than raising into the agent.
    assert current_turn() is None
