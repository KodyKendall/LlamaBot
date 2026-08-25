"""Gap 2: rails_beginner_agent is a raw StateGraph node (no middleware.py), so it
invokes the model directly and — before this fix — had NO transient-error retry at
all. 4 real turns died on a single `httpx.RemoteProtocolError` ("peer closed
connection ... incomplete chunked read") that the resilience ladder should have
eaten. This test drives the actual node and asserts a model that raises a
transient error twice then succeeds produces a response, not a raise.

We call `leonardo_beginner` directly rather than building the graph: build_workflow()
clears the asyncio event loop (team CI memo), and the node is where the invoke lives.
"""
import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import app.agents.leonardo.rails_beginner_agent.nodes as nb
import app.agents.leonardo.resilience as res


class _FlakyBoundModel:
    """The object llm.bind_tools() returns: .invoke raises a transient error
    `fail_times` times, then returns a plain AIMessage."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)"
            )
        return AIMessage(content="all done!")


class _FlakyModel:
    """Stand-in for get_llm(...): bind_tools returns the same flaky bound model so
    we can count invokes across the retry loop."""

    def __init__(self, fail_times):
        self.bound = _FlakyBoundModel(fail_times)

    def bind_tools(self, *a, **k):
        return self.bound

    # the failure-limit branch invokes the bare model (no bind_tools)
    def invoke(self, messages, **kwargs):
        return self.bound.invoke(messages, **kwargs)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(res.time, "sleep", lambda *_: None)
    # Isolate the retry behavior from prompt/brand/skill machinery.
    monkeypatch.setattr(nb, "get_sys_msg", lambda: {"role": "system", "content": "sys"})
    monkeypatch.setattr(nb, "normalize_messages_for_provider", lambda m: m)


def test_beginner_node_retries_transient_then_succeeds(monkeypatch):
    flaky = _FlakyModel(fail_times=2)
    monkeypatch.setattr(nb, "get_llm", lambda name: flaky)

    state = {"messages": [HumanMessage(content="build me a page")]}
    out = nb.leonardo_beginner(state)

    assert out["messages"][0].content == "all done!"   # response, not a raise
    assert flaky.bound.calls == 3                       # failed twice, succeeded on 3rd


def test_beginner_node_gives_up_after_cap(monkeypatch):
    """A persistently transient endpoint still re-raises after the cap so it can
    reach the outer floor rung — it must not retry forever."""
    flaky = _FlakyModel(fail_times=99)
    monkeypatch.setattr(nb, "get_llm", lambda name: flaky)

    state = {"messages": [HumanMessage(content="build me a page")]}
    with pytest.raises(httpx.RemoteProtocolError):
        nb.leonardo_beginner(state)
    assert flaky.bound.calls == res._MODEL_RETRY_MAX_ATTEMPTS
