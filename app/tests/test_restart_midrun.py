"""Reproduction: what an in-place update does to a LangGraph run that is streaming.

`bin/update` recreates the llamabot container, so the process holding the
background run dies mid-turn. These tests pin what that actually costs:

1. The run is orphaned — RunManager state is process-local, so the reconnecting
   client is told `no_active_run` and cannot replay.
2. Nothing on the Python side reports it. The run dies by cancellation, which is
   the one path RunManager deliberately swallows, and `graceful_shutdown` never
   looks at the run manager at all.

Run with: pytest app/tests/test_restart_midrun.py -v
"""
import asyncio
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from app.websocket.run_manager import RunManager
from app.websocket.web_socket_handler import WebSocketHandler


class FakeWS:
    def __init__(self):
        self.sent = []
        self.client_state = WebSocketState.CONNECTED

    async def send_json(self, msg):
        self.sent.append(msg)


class FakeManager:
    """Stands in for WebSocketConnectionManager: carries `app`, records sends."""

    def __init__(self):
        self.app = SimpleNamespace(state=SimpleNamespace())
        self.sent = []

    async def send_personal_message(self, msg, websocket):
        self.sent.append(msg)


async def _mid_stream_run(started, release):
    """A run that emits one frame, then blocks — i.e. is streaming when we kill it."""

    async def factory(sink):
        await sink.send_json({"type": "ai", "content": "half an answer"})
        started.set()
        await release.wait()          # the container is recreated right here
        await sink.send_json({"type": "end"})

    return factory


# ------------------------------------------------------- 1. the run is orphaned

@pytest.mark.asyncio
async def test_restart_orphans_the_inflight_run():
    """A fresh process has no memory of the run that was streaming."""
    started, release = asyncio.Event(), asyncio.Event()
    ws = FakeWS()

    old_process = RunManager()
    await old_process.start("thread-1", await _mid_stream_run(started, release), ws)
    await asyncio.wait_for(started.wait(), timeout=2)

    # Before the restart, a reconnecting client attaches and replays.
    handle = old_process.attach("thread-1", ws)
    assert handle is not None
    assert handle.log.status == "running"
    assert [e["content"] for e in handle.log.since(0)] == ["half an answer"]

    # SIGTERM: the event loop and every task on it go away with the process.
    for task in old_process.active_tasks():
        task.cancel()
    await asyncio.gather(*old_process.active_tasks(), return_exceptions=True)

    # The replacement container starts with an empty registry.
    new_process = RunManager()
    assert new_process.attach("thread-1", FakeWS()) is None, (
        "RunManager is in-memory; a restart must lose the run (documented, not a bug)"
    )


@pytest.mark.asyncio
async def test_attach_after_restart_tells_the_client_no_active_run():
    """The exact frame the browser gets when it reconnects post-update."""
    manager = FakeManager()
    ws = FakeWS()
    handler = WebSocketHandler(ws, manager)

    # Fresh process: run_manager is lazily created and empty.
    await handler._handle_attach({"type": "attach", "thread_id": "thread-1", "last_seq": 7})

    assert manager.sent == [{"type": "no_active_run", "thread_id": "thread-1"}]
    # Not `replay_gap` — there is no log at all, so there is nothing to resume.


@pytest.mark.asyncio
async def test_partial_output_is_all_the_client_ever_sees():
    """The killed turn stops mid-stream: no `end`, no error frame, just silence."""
    started, release = asyncio.Event(), asyncio.Event()
    ws = FakeWS()

    rm = RunManager()
    await rm.start("thread-1", await _mid_stream_run(started, release), ws)
    await asyncio.wait_for(started.wait(), timeout=2)

    await rm.cancel("thread-1")

    types = [m["type"] for m in ws.sent]
    assert types == ["ai"], f"expected only the pre-kill frame, got {types}"
    assert "end" not in types and "error" not in types


# ------------------------------------------------- 2. nothing is reported (Python)

@pytest.mark.asyncio
async def test_a_killed_run_is_swallowed_not_reported():
    """Cancellation marks the log 'cancelled' and raises nothing a reporter could catch."""
    started, release = asyncio.Event(), asyncio.Event()
    rm = RunManager()
    await rm.start("thread-1", await _mid_stream_run(started, release), FakeWS())
    await asyncio.wait_for(started.wait(), timeout=2)

    handle = rm._runs["thread-1"]
    await rm.cancel("thread-1")

    assert handle.log.status == "cancelled"
    assert handle.task.cancelled()


def test_run_manager_has_no_error_telemetry_path():
    """No rung of the error ladder is wired into the run lifecycle."""
    import app.websocket.run_manager as rm_module

    source = inspect.getsource(rm_module)
    assert "report_error" not in source
    assert "mothership" not in source.lower()


def test_graceful_shutdown_never_drains_or_reports_runs():
    """SIGTERM handling ignores in-flight runs entirely."""
    main_py = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text()
    body = re.search(
        r"async def graceful_shutdown\(sig\):.*?(?=\n@app\.|\nasync def |\ndef )",
        main_py,
        re.S,
    )
    assert body, "graceful_shutdown moved — update this test"
    body = body.group(0)

    assert "notify_teardown" in body, "it does ping the mothership..."
    assert "run_manager" not in body, "...but never looks at the in-flight runs"
    assert "active_tasks" not in body
    assert "report_error" not in body
