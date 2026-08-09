"""Tests for TurnMetricsMiddleware — the per-model-call / per-tool-call timer.

This is the chokepoint that answers "is the provider actually decoding slower?".
It must (a) measure, (b) never change what the agent sees, and (c) be wired into
EVERY create_agent agent so no mode can silently lose timing.

The middleware is exercised against lightweight request stubs rather than real
ModelRequest dataclasses: it touches only ``request.model`` / ``request.override``
/ ``request.tool_call``, and stubbing keeps these tests free of a compiled graph
(building one clears the asyncio event loop in CI).
"""
import asyncio

import pytest

from app.agents.leonardo.turn_metrics_middleware import TurnMetricsMiddleware
from app.lib.turn_metrics import current_turn, start_turn


class _FakeClock:
    """Monotonic clock we advance by hand, so tests never sleep."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Msg:
    def __init__(self, usage_metadata=None):
        self.usage_metadata = usage_metadata
        self.content = "hi"


class _Response:
    def __init__(self, messages):
        self.result = messages


class _ModelStub:
    """Stands in for a chat model; records callbacks handed to with_config."""

    def __init__(self):
        self.callbacks = None

    def with_config(self, config):
        self.callbacks = (config or {}).get("callbacks")
        return self


class _RequestStub:
    def __init__(self):
        self.model = _ModelStub()
        self.overridden_with = None

    def override(self, **kwargs):
        self.overridden_with = kwargs
        if "model" in kwargs:
            self.model = kwargs["model"]
        return self


class _ToolRequestStub:
    def __init__(self, name="write_file"):
        self.tool_call = {"name": name, "args": {}, "id": "call_1"}


def test_records_model_call_duration_and_tokens():
    clock = _FakeClock()
    mw = TurnMetricsMiddleware(time_fn=clock)

    async def handler(request):
        clock.advance(2.5)
        return _Response([_Msg({"input_tokens": 900, "output_tokens": 120, "total_tokens": 1020})])

    async def main():
        turn = start_turn(thread_id="t", agent_mode="rails_agent")
        await mw.awrap_model_call(_RequestStub(), handler)
        return turn.snapshot()

    snap = asyncio.run(main())
    assert snap["model_ms"] == 2500
    assert snap["output_tokens"] == 120
    assert snap["input_tokens"] == 900
    assert snap["model_calls"] == 1


def test_records_tool_call_duration_and_name():
    clock = _FakeClock()
    mw = TurnMetricsMiddleware(time_fn=clock)

    async def handler(request):
        clock.advance(1.25)
        return "tool output"

    async def main():
        turn = start_turn()
        await mw.awrap_tool_call(_ToolRequestStub("browser_inspect"), handler)
        return turn.snapshot()

    snap = asyncio.run(main())
    assert snap["tool_ms"] == 1250
    assert snap["slowest_tool"] == {"name": "browser_inspect", "ms": 1250}


def test_returns_the_handler_result_unchanged():
    # Instrumentation must be transparent: whatever the model or tool produced
    # is what the agent gets back.
    mw = TurnMetricsMiddleware(time_fn=_FakeClock())
    sentinel = _Response([_Msg()])

    async def handler(request):
        return sentinel

    async def main():
        start_turn()
        return await mw.awrap_model_call(_RequestStub(), handler)

    assert asyncio.run(main()) is sentinel


def test_records_duration_even_when_the_call_raises():
    # A slow failure is a performance signal too — losing it would hide the
    # exact turns users complain hardest about.
    clock = _FakeClock()
    mw = TurnMetricsMiddleware(time_fn=clock)

    async def handler(request):
        clock.advance(30.0)
        raise RuntimeError("provider timeout")

    async def main():
        turn = start_turn()
        with pytest.raises(RuntimeError):
            await mw.awrap_model_call(_RequestStub(), handler)
        return turn.snapshot()

    assert asyncio.run(main())["model_ms"] == 30000


def test_is_a_noop_when_no_turn_is_active():
    # Headless executor, background jobs and tests run agents with no turn
    # recording installed; the middleware must pass straight through.
    mw = TurnMetricsMiddleware(time_fn=_FakeClock())
    sentinel = _Response([_Msg()])

    async def handler(request):
        return sentinel

    async def main():
        assert current_turn() is None
        return await mw.awrap_model_call(_RequestStub(), handler)

    assert asyncio.run(main()) is sentinel


def test_attaches_a_first_token_callback_to_the_model():
    # Per-call TTFT is what separates prefill (grows with thread length) from
    # decode rate (the provider's actual speed).
    clock = _FakeClock()
    mw = TurnMetricsMiddleware(time_fn=clock)
    request = _RequestStub()

    async def handler(req):
        clock.advance(2.0)
        for cb in req.model.callbacks:
            cb.on_llm_new_token("tok")  # first token lands 2s in
        clock.advance(3.0)
        return _Response([_Msg({"input_tokens": 1, "output_tokens": 300, "total_tokens": 301})])

    async def main():
        turn = start_turn()
        await mw.awrap_model_call(request, handler)
        return turn

    turn = asyncio.run(main())
    last = turn.last_model_call()
    assert last["ttft_ms"] == 2000
    assert last["duration_ms"] == 5000
    # 300 tokens over the 3s of decode time, not the full 5s call.
    assert last["tokens_per_second"] == pytest.approx(100.0)


def test_a_broken_model_never_blocks_the_call():
    # If with_config is unavailable on some model wrapper we lose TTFT for that
    # call — we do not lose the user's turn.
    clock = _FakeClock()
    mw = TurnMetricsMiddleware(time_fn=clock)

    class _Hostile(_RequestStub):
        def override(self, **kwargs):
            raise TypeError("override not supported here")

    async def handler(request):
        clock.advance(1.0)
        return _Response([_Msg()])

    async def main():
        turn = start_turn()
        await mw.awrap_model_call(_Hostile(), handler)
        return turn.snapshot()

    assert asyncio.run(main())["model_ms"] == 1000


def test_every_leonardo_agent_gets_the_middleware(monkeypatch):
    # Wiring guard: build_leonardo_agent is the single chokepoint all 11 modes
    # go through. If timing is not added here, individual modes lose it
    # silently and the fleet data has holes we cannot see.
    from app.agents.leonardo import agent_factory

    captured = {}

    def fake_create_agent(**kwargs):
        captured["middleware"] = kwargs.get("middleware")
        return "agent"

    monkeypatch.setattr(agent_factory, "create_agent", fake_create_agent)
    agent_factory.build_leonardo_agent(model="x", tools=[])

    assert any(isinstance(m, TurnMetricsMiddleware) for m in captured["middleware"])


def test_middleware_is_not_added_twice(monkeypatch):
    from app.agents.leonardo import agent_factory

    captured = {}

    def fake_create_agent(**kwargs):
        captured["middleware"] = kwargs.get("middleware")
        return "agent"

    monkeypatch.setattr(agent_factory, "create_agent", fake_create_agent)
    agent_factory.build_leonardo_agent(model="x", tools=[], middleware=[TurnMetricsMiddleware()])

    assert sum(isinstance(m, TurnMetricsMiddleware) for m in captured["middleware"]) == 1
