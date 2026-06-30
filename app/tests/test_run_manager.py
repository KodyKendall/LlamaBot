"""Tests for Layer 2: background runs + reconnect replay.

Core guarantees:
- A run keeps going after its socket detaches (the build doesn't die on disconnect).
- Output is logged with monotonic seq and replayable via since(last_seq).
- A reconnecting subscriber replays missed messages, then live-tails.
- A new message supersedes (cancels) the prior run for the same thread.
"""
import asyncio
import pytest
from starlette.websockets import WebSocketState

from app.websocket.run_manager import (
    ThreadOutputLog,
    RunHandle,
    RunSink,
    RunManager,
)


class FakeWS:
    """Minimal websocket double: records send_json payloads; state is toggleable."""

    def __init__(self):
        self.sent = []
        self.client_state = WebSocketState.CONNECTED

    async def send_json(self, msg):
        if self.client_state != WebSocketState.CONNECTED:
            raise RuntimeError("socket closed")
        self.sent.append(msg)

    def close(self):
        self.client_state = WebSocketState.DISCONNECTED


# ---------------------------------------------------------------- ThreadOutputLog

def test_log_assigns_monotonic_seq():
    log = ThreadOutputLog()
    a = log.append({"type": "ai", "content": "one"})
    b = log.append({"type": "ai", "content": "two"})
    assert a["seq"] == 1 and b["seq"] == 2
    assert log.last_seq == 2


def test_log_since_returns_only_newer():
    log = ThreadOutputLog()
    for i in range(5):
        log.append({"type": "ai", "content": i})
    got = log.since(3)
    assert [e["seq"] for e in got] == [4, 5]


def test_log_eviction_and_gap_detection():
    log = ThreadOutputLog(maxlen=3)
    for i in range(5):
        log.append({"type": "ai", "content": i})  # seqs 1..5, only 3,4,5 retained
    assert log.min_seq == 3
    # Client last saw seq 1 → seq 2 was evicted → gap.
    assert log.has_gap(1) is True
    # Client last saw seq 4 → only seq 5 missing, still retained → no gap.
    assert log.has_gap(4) is False


# ----------------------------------------------------------------------- RunSink

@pytest.mark.asyncio
async def test_sink_logs_and_forwards_to_attached_socket():
    ws = FakeWS()
    handle = RunHandle("t1", ThreadOutputLog())
    handle.attached_ws = ws
    sink = RunSink(handle)

    await sink.send_json({"type": "ai", "content": "hi"})

    assert handle.log.last_seq == 1
    assert ws.sent[0]["content"] == "hi"
    assert ws.sent[0]["seq"] == 1  # forwarded copy carries the seq


@pytest.mark.asyncio
async def test_sink_logs_when_no_socket_attached():
    handle = RunHandle("t1", ThreadOutputLog())
    sink = RunSink(handle)  # attached_ws is None
    await sink.send_json({"type": "ai", "content": "hi"})
    assert handle.log.last_seq == 1  # logged for later replay


@pytest.mark.asyncio
async def test_sink_run_continues_when_forward_raises():
    ws = FakeWS()
    ws.close()  # send_json will raise
    handle = RunHandle("t1", ThreadOutputLog())
    handle.attached_ws = ws
    sink = RunSink(handle)

    # Must not raise — the run keeps going, message stays in the log.
    await sink.send_json({"type": "ai", "content": "hi"})
    assert handle.log.last_seq == 1


@pytest.mark.asyncio
async def test_sink_reports_connected_for_is_open_checks():
    sink = RunSink(RunHandle("t1", ThreadOutputLog()))
    assert sink.client_state == WebSocketState.CONNECTED


# --------------------------------------------------------------------- RunManager

@pytest.mark.asyncio
async def test_run_continues_after_socket_detaches():
    """The build keeps running and logging even after the browser drops."""
    rm = RunManager()
    ws = FakeWS()
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory(sink):
        await sink.send_json({"type": "ai", "content": "chunk-1"})
        started.set()
        await release.wait()  # simulate a long-running build
        await sink.send_json({"type": "ai", "content": "chunk-2"})
        await sink.send_json({"type": "end"})

    handle = await rm.start("t1", factory, websocket=ws)
    await started.wait()
    assert ws.sent[0]["content"] == "chunk-1"

    # Browser drops mid-run.
    ws.close()
    rm.detach("t1", ws)

    # The run finishes on its own.
    release.set()
    await handle.task

    assert handle.log.status == "done"
    # All three messages are in the log even though the socket was gone for 2 of them.
    assert [e.get("content", e.get("type")) for e in handle.log.since(0)] == [
        "chunk-1", "chunk-2", "end",
    ]


@pytest.mark.asyncio
async def test_reconnect_replays_missed_messages():
    rm = RunManager()
    ws1 = FakeWS()
    release = asyncio.Event()
    mid = asyncio.Event()

    async def factory(sink):
        await sink.send_json({"type": "ai", "content": "c1"})
        mid.set()
        await release.wait()
        await sink.send_json({"type": "ai", "content": "c2"})

    handle = await rm.start("t1", factory, websocket=ws1)
    await mid.wait()
    ws1.close()
    rm.detach("t1", ws1)

    # Reconnect: new socket attaches, replays everything after last_seq=1.
    ws2 = FakeWS()
    h = rm.attach("t1", ws2)
    assert h is handle
    missed = handle.log.since(1)
    assert missed == []  # nothing emitted after seq 1 yet

    # Now the run emits c2 — it live-tails to ws2.
    release.set()
    await handle.task
    assert ws2.sent[-1]["content"] == "c2"


@pytest.mark.asyncio
async def test_new_message_supersedes_prior_run():
    rm = RunManager()
    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def long_run(sink):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    await rm.start("t1", long_run)
    await started.wait()

    async def quick(sink):
        await sink.send_json({"type": "ai", "content": "new"})

    # Starting a new run for the same thread cancels the old one first.
    handle = await rm.start("t1", quick)
    await handle.task
    assert cancelled.is_set()
    assert handle.log.status == "done"
    # seq is monotonic across the supersede (log reused).
    assert handle.log.last_seq >= 1


@pytest.mark.asyncio
async def test_explicit_cancel_stops_run():
    rm = RunManager()
    started = asyncio.Event()

    async def long_run(sink):
        started.set()
        await asyncio.sleep(10)

    await rm.start("t1", long_run)
    await started.wait()
    assert await rm.cancel("t1") is True
    assert rm.get("t1").log.status == "cancelled"
    # Cancelling an already-finished/absent run is a no-op.
    assert await rm.cancel("nonexistent") is False


@pytest.mark.asyncio
async def test_detach_does_not_cancel():
    rm = RunManager()
    ws = FakeWS()
    started = asyncio.Event()

    async def run(sink):
        started.set()
        await asyncio.sleep(0.05)
        await sink.send_json({"type": "end"})

    handle = await rm.start("t1", run, websocket=ws)
    await started.wait()
    rm.detach("t1", ws)  # disconnect
    await handle.task
    assert handle.log.status == "done"  # ran to completion despite detach


@pytest.mark.asyncio
async def test_finished_runs_evicted_under_pressure_running_kept():
    rm = RunManager(max_threads=2)

    async def quick(sink):
        await sink.send_json({"type": "end"})

    h1 = await rm.start("t1", quick)
    await h1.task
    h2 = await rm.start("t2", quick)
    await h2.task
    # Third finished run should evict an older finished one, staying within bound.
    h3 = await rm.start("t3", quick)
    await h3.task
    assert len(rm._runs) <= 2
