"""Background LangGraph runs + reconnect replay (Layer 2).

See docs/dev/websocket_background_runs.md for the full design. In short:

- A chat turn (or a resume after approval/question) runs as a **background task
  keyed by thread_id**, owned by the app-level :class:`RunManager` — NOT by the
  WebSocket connection that started it. So when the browser drops, the build
  keeps running.
- Every message the run would have sent to the socket is written to a per-thread
  :class:`ThreadOutputLog` (assigning a monotonic ``seq``) and *also* forwarded
  to the currently-attached live socket, if any. The socket is a subscriber, not
  the run's lifeline.
- On reconnect the client sends ``{type:"attach", thread_id, last_seq}``; the
  handler replays ``log.since(last_seq)`` then live-tails. The client dedupes by
  ``seq`` so replay/live-tail overlap is harmless.

The run writes through :class:`RunSink`, which deliberately mimics a Starlette
WebSocket (``send_json`` + ``client_state``) so the existing streaming code in
RequestHandler needs no per-send-site changes: ``_is_websocket_open(sink)`` is
always True (the run never gates on a live socket), and ``sink.send_json(...)``
logs + forwards.

This is process-local (in-memory). It is NOT durable across a process restart —
a client that reconnects after a restart gets ``no_active_run`` and falls back to
loading thread history from the checkpointer. Durable (Redis/Postgres) replay is
a later step; see the design doc.
"""
import asyncio
from asyncio import CancelledError
from collections import deque, OrderedDict
import contextvars
from dataclasses import dataclass
import logging
import os
import time
import uuid

from langchain_core.runnables.config import var_child_runnable_config
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)


def _top_level_context() -> contextvars.Context:
    """The current context, minus any enclosing LangChain/LangGraph run.

    Research is launched from inside a tool call, and the results turn from
    inside the research task. A task inherits its creator's context, including
    LangChain's parent-run config, so without this a new turn runs as if nested
    in that old, finished graph run: the model answers, but not one checkpoint
    lands on the thread (seen live 2026-10-22). The user identity in the context
    (who the turn is for, whose ChatGPT plan pays) must still carry over.
    """
    ctx = contextvars.copy_context()
    ctx.run(var_child_runnable_config.set, None)
    return ctx


class ThreadOutputLog:
    """Bounded, sequence-numbered log of the messages a run emitted for a thread.

    ``seq`` is monotonic across turns for the lifetime of the handle, so a client
    can carry a single ``last_seq`` across reconnects and even across turns.
    """

    def __init__(self, maxlen: int = 2000):
        self._entries = deque(maxlen=maxlen)  # each entry is the stored msg dict (carries "seq")
        self._seq = 0
        # Run lifecycle as seen by subscribers: running -> done | error | cancelled.
        self.status = "running"

    def append(self, msg: dict) -> dict:
        """Assign the next seq, store a copy carrying it, and return that copy."""
        self._seq += 1
        stored = {**msg, "seq": self._seq}
        self._entries.append(stored)
        return stored

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def min_seq(self) -> int:
        """Lowest seq still retained (0 if empty). Below this, entries were evicted."""
        return self._entries[0]["seq"] if self._entries else 0

    def since(self, last_seq: int) -> list:
        """Every retained entry with seq > last_seq, in order."""
        return [e for e in self._entries if e["seq"] > last_seq]

    def has_gap(self, last_seq: int) -> bool:
        """True if the client is missing entries that have already been evicted.

        The client wants everything after ``last_seq``. If the oldest entry we
        still hold is newer than ``last_seq + 1``, the messages in between are
        gone and replay would be incomplete — the client must reload from the
        checkpointer instead.
        """
        if not self._entries:
            return False
        return self.min_seq > last_seq + 1


@dataclass
class QueuedMessage:
    """A chat frame waiting for the thread's running turn to pick it up.

    ``notification`` marks a server-made message (background research results),
    so it skips the paywall and is not reported as something the user typed. It
    is a field here rather than a frame key so a client can never set it.
    """
    frame: dict
    notification: bool = False
    # Held for the next model call only (never starts a turn by itself) — see requeue().
    held: bool = False


class RunHandle:
    """One thread's run: its task, its output log, and the live socket (if any)."""

    def __init__(self, thread_id: str, log: ThreadOutputLog):
        self.thread_id = thread_id
        self.log = log
        self.task = None          # asyncio.Task running the graph
        self.attached_ws = None   # the currently-live subscriber socket, or None
        self.pending = []         # QueuedMessages sent while a turn was running
        self.factory_for = None   # builds the run that picks up leftover pending
        self.background = {}      # task_id -> {"task", "kind", "label", "started_at"}
        # The user's words this turn answers — the auto-save's commit title (LP-46).
        # A notification or resume turn keeps the request it belongs to.
        self.turn_request = None

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()


class RunSink:
    """WebSocket-shaped sink: logs every send and forwards to the live socket.

    Mimics enough of a Starlette WebSocket that RequestHandler's streaming code
    works unchanged:
    - ``client_state`` is always CONNECTED, so ``_is_websocket_open(sink)`` is
      True and the run never skips a send or aborts mid-stream.
    - ``send_json`` appends to the log (assigning a seq) then best-effort
      forwards the stored copy to the attached socket. ``thread_id`` lets
      RequestHandler key its per-thread lock off the sink.
    """

    def __init__(self, handle: RunHandle):
        self._handle = handle
        self.client_state = WebSocketState.CONNECTED
        self.thread_id = handle.thread_id

    async def send_json(self, msg: dict) -> None:
        # Label every frame with the thread it belongs to. Runs are per-thread and
        # several can stream at once (multiple tabs, the ticket→engineer handoff),
        # all forwarding to whatever socket is attached. Without a thread_id the
        # client renders each frame into whichever chat is on screen — so one
        # thread's output bleeds into another. Don't clobber a thread_id a caller
        # already set (interrupt frames carry their own).
        if "thread_id" not in msg:
            msg = {**msg, "thread_id": self.thread_id}
        stored = self._handle.log.append(msg)
        ws = self._handle.attached_ws
        if ws is None:
            return
        try:
            if getattr(ws, "client_state", None) == WebSocketState.CONNECTED:
                await ws.send_json(stored)
        except Exception as e:
            # The live socket died mid-send; the run continues and the message is
            # safe in the log for replay on the next attach.
            logger.debug(f"RunSink forward failed (run continues): {e}")

    async def send_text(self, text: str) -> None:
        await self.send_json({"type": "text", "content": text})


class RunManager:
    """App-level registry of background runs, one per thread_id.

    Shared across every WebSocket connection (lives on ``app.state``) so a
    reconnect — which creates a fresh handler — can still find and attach to a
    run started by the previous connection.
    """

    def __init__(self, log_maxlen: int = 2000, max_threads: int = 512,
                 after_turn=None, before_turn=None):
        self._runs: "OrderedDict[str, RunHandle]" = OrderedDict()
        # LP-46: ``before_turn()`` and ``after_turn(request, thread_id)`` run in a
        # worker thread around every turn, including ones that error or are stopped.
        # Only the app's shared manager has them (the auto-save); tests build bare ones.
        self._after_turn = after_turn
        self._before_turn = before_turn
        self._start_locks: dict = {}
        self._log_maxlen = log_maxlen
        self._max_threads = max_threads

    def get(self, thread_id) -> RunHandle:
        return self._runs.get(str(thread_id))

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        if thread_id not in self._start_locks:
            self._start_locks[thread_id] = asyncio.Lock()
        return self._start_locks[thread_id]

    def _handle_for(self, thread_id: str) -> RunHandle:
        handle = self._runs.get(thread_id)
        if handle is None:
            handle = RunHandle(thread_id, ThreadOutputLog(maxlen=self._log_maxlen))
            handle.log.status = "done"
            self._runs[thread_id] = handle
        return handle

    async def start(self, thread_id, run_factory, websocket=None) -> RunHandle:
        """Start a background run for ``thread_id``, superseding any active one.

        ``run_factory`` is ``async (sink) -> None`` — typically a closure over a
        RequestHandler streaming method. Used for resumes (approval / question
        answers), where the thread is paused, not running. Chat messages go
        through :meth:`submit`, which queues instead of cancelling. The output
        log is REUSED across turns so ``seq`` stays monotonic and a client can
        attach with a single ``last_seq``.
        """
        thread_id = str(thread_id)
        async with self._lock_for(thread_id):
            existing = self._runs.get(thread_id)
            if existing and existing.is_running():
                existing.task.cancel()
                try:
                    await existing.task
                except (CancelledError, Exception):
                    pass
            return self._start_locked(thread_id, run_factory, websocket)

    async def submit(self, thread_id, item: QueuedMessage, factory_for, websocket=None) -> str:
        """Run a chat message now, or queue it if the thread is mid-turn.

        A new message NEVER cancels the running turn — only Stop does. Queued
        messages reach the agent one of two ways: QueuedMessagesMiddleware drains
        them before the next model call, or, if the turn ends first, they start
        the next turn together. ``factory_for(items)`` builds that run.

        Returns ``"started"`` or ``"queued"``.
        """
        thread_id = str(thread_id)
        async with self._lock_for(thread_id):
            handle = self._runs.get(thread_id)
            if handle is not None and handle.is_running():
                handle.pending.append(item)
                handle.factory_for = factory_for
                if websocket is not None:
                    handle.attached_ws = websocket
                return "queued"
            _note_request(handle or self._handle_for(thread_id), [item])
            handle = self._start_locked(thread_id, factory_for([item]), websocket)
            handle.factory_for = factory_for
            return "started"

    def drain(self, thread_id) -> list:
        """Take every queued message for the thread (the running turn consumes them)."""
        handle = self._runs.get(str(thread_id))
        if handle is None or not handle.pending:
            return []
        items, handle.pending = handle.pending, []
        return items

    def requeue(self, thread_id, item: QueuedMessage) -> None:
        """Hold a message for the thread's next model call, without starting a turn.

        Used when the thread is paused on a question card: the message must not
        resume it, so it waits until the user's answer runs the agent again.
        """
        item.held = True
        self._handle_for(str(thread_id)).pending.append(item)

    def _start_locked(self, thread_id: str, run_factory, websocket) -> RunHandle:
        handle = self._handle_for(thread_id)
        handle.log.status = "running"
        if websocket is not None:
            handle.attached_ws = websocket

        sink = RunSink(handle)
        handle.task = asyncio.create_task(
            self._wrap(handle, run_factory, sink), context=_top_level_context(),
        )

        self._runs.move_to_end(thread_id)
        self._evict_finished_if_needed()
        return handle

    async def _wrap(self, handle: RunHandle, run_factory, sink: RunSink) -> None:
        try:
            # Lets the client show "working" for a turn it did not start itself
            # (a queued follow-up, or research results arriving).
            await sink.send_json({"type": "run_started"})
            await self._call_hook(self._before_turn)
            await run_factory(sink)
            handle.log.status = "done"
        except CancelledError:
            handle.log.status = "cancelled"
            raise
        except Exception:
            handle.log.status = "error"
            logger.exception(f"Background run failed for thread {handle.thread_id}")
            # Swallow: the error message was already streamed to the sink by the
            # run body's own except handler. Re-raising would only produce an
            # unretrieved-task-exception warning.
        finally:
            # After the reply is out: save the turn's file changes (LP-46).
            await self._save_after_turn(handle, sink)
            # Messages that arrived after the turn's last model call: they start
            # the next turn. Not after Stop — cancel() already dropped them.
            if any(not i.held for i in handle.pending) and handle.log.status != "cancelled":
                asyncio.create_task(self._run_pending(handle.thread_id))

    async def _call_hook(self, hook, *args):
        """Run a turn hook off the event loop. Never raises: a hook can't break a turn."""
        if hook is None:
            return None
        try:
            return await asyncio.to_thread(hook, *args)
        except Exception:
            logger.exception("Turn hook failed (turn unaffected)")
            return None

    async def _save_after_turn(self, handle: RunHandle, sink: "RunSink") -> None:
        if self._after_turn is None:
            return
        result = await self._call_hook(self._after_turn, handle.turn_request, handle.thread_id)
        try:
            # Tells the History panel to re-read its status now, not on the next poll.
            await sink.send_json({"type": "save_status",
                                  "saved": bool(result and not result.get("skipped"))})
        except Exception:
            logger.debug("save_status frame not sent", exc_info=True)

    def busy(self) -> bool:
        """Is any turn or coding task running? (The History panel's "Leo working".)"""
        return any(h.is_running() for h in self._runs.values()) or self.writers_busy()

    async def _run_pending(self, thread_id: str) -> None:
        async with self._lock_for(thread_id):
            handle = self._runs.get(thread_id)
            # A turn that is already running will drain the queue itself.
            if handle is None or handle.is_running():
                return
            items = [i for i in handle.pending if not i.held]
            if not items:
                return
            handle.pending = [i for i in handle.pending if i.held]
            _note_request(handle, items)
            self._start_locked(thread_id, handle.factory_for(items), None)

    # ------------------------------------------------------- background tasks

    def start_background(self, thread_id, work, *, kind: str, label: str,
                         max_per_thread: int = 3):
        """Run ``work()`` on the thread, outside any turn. Returns a task id.

        The task belongs to the RunManager, not to the turn that launched it, so
        the turn can end (and the user can keep chatting) while it runs. The
        client gets ``background_task`` frames (running / finished) for the
        status pill. Returns None when the thread is at ``max_per_thread``.
        In memory only: a process restart loses in-flight tasks.
        """
        thread_id = str(thread_id)
        handle = self._handle_for(thread_id)
        if len(handle.background) >= max_per_thread:
            return None
        task_id = uuid.uuid4().hex[:8]
        info = {"kind": kind, "label": label, "started_at": time.time()}
        handle.background[task_id] = info
        sink = RunSink(handle)

        async def frame(status: str) -> None:
            await sink.send_json({
                "type": "background_task", "status": status, "task_id": task_id,
                "kind": kind, "label": label, "started_at": info["started_at"],
            })

        async def run() -> None:
            await frame("running")
            try:
                await work()
            except CancelledError:
                raise
            except Exception:
                logger.exception(f"Background {kind} {task_id} failed on thread {thread_id}")
            finally:
                handle.background.pop(task_id, None)
                await frame("finished")

        info["task"] = asyncio.create_task(run(), context=_top_level_context())
        return task_id

    def _writer_tasks(self) -> list:
        """Running coding tasks on ANY thread — they all edit the same files."""
        return [i["task"] for h in self._runs.values()
                for i in h.background.values() if i["kind"] == "coding"]

    def writers_busy(self) -> bool:
        return bool(self._writer_tasks())

    async def wait_for_writers(self) -> None:
        """Return once no coding task is editing files (the writer gate)."""
        while tasks := self._writer_tasks():
            await asyncio.wait(tasks)

    def background_tasks(self, thread_id) -> list:
        """The thread's running background tasks, as plain dicts (for the client)."""
        handle = self._runs.get(str(thread_id))
        if handle is None:
            return []
        return [
            {"task_id": tid, "kind": i["kind"], "label": i["label"], "started_at": i["started_at"]}
            for tid, i in handle.background.items()
        ]

    def attach(self, thread_id, websocket) -> RunHandle:
        """Mark ``websocket`` as the live subscriber for the thread's run.

        Returns the RunHandle (so the caller can replay ``log.since(...)``), or
        None if there is no run for this thread (client should reload history).
        """
        handle = self._runs.get(str(thread_id))
        if handle is not None:
            handle.attached_ws = websocket
            self._runs.move_to_end(str(thread_id))
        return handle

    def detach(self, thread_id, websocket) -> None:
        """Clear the live socket on disconnect WITHOUT cancelling the run."""
        handle = self._runs.get(str(thread_id))
        if handle is not None and handle.attached_ws is websocket:
            handle.attached_ws = None

    async def cancel(self, thread_id) -> bool:
        """Explicitly cancel a thread's work (user pressed stop).

        Stop means stop: the running turn, anything queued behind it, and any
        background research on the thread.
        """
        handle = self._runs.get(str(thread_id))
        if handle is None:
            return False
        handle.pending = []
        tasks = [i["task"] for i in handle.background.values()]
        if handle.is_running():
            tasks.insert(0, handle.task)
        if not tasks:
            return False
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (CancelledError, Exception):
                pass
        return True

    def active_tasks(self) -> list:
        """All not-yet-finished run tasks (used for graceful shutdown / tests)."""
        tasks = [h.task for h in self._runs.values() if h.is_running()]
        tasks += [i["task"] for h in self._runs.values() for i in h.background.values()]
        return tasks

    def _evict_finished_if_needed(self) -> None:
        """Bound memory: drop oldest FINISHED runs once we exceed max_threads.

        Never evicts a still-running run. Replaces the design's TTL with a
        simpler LRU bound (a reconnect to an evicted finished run gets
        ``no_active_run`` and reloads history — same fallback as a gap).
        """
        while len(self._runs) > self._max_threads:
            for tid, handle in list(self._runs.items()):
                if not handle.is_running() and not handle.background and not handle.pending:
                    del self._runs[tid]
                    self._start_locks.pop(tid, None)
                    break
            else:
                break  # nothing finished to evict; let it grow rather than kill a live run


_shared_run_manager = None


def shared_run_manager() -> RunManager:
    """The process-wide RunManager (main.py runs twice: as ``main`` and ``app.main``)."""
    global _shared_run_manager
    if _shared_run_manager is None:
        _shared_run_manager = RunManager(after_turn=autosave_hook, before_turn=autosave_turn_start_hook)
    return _shared_run_manager


def _note_request(handle: RunHandle, items: list) -> None:
    """Remember the first thing the user typed in these items, for the save's title."""
    for item in items:
        if not item.notification and item.frame.get("message"):
            handle.turn_request = str(item.frame["message"])
            return


def _autosave_off() -> bool:
    """Read every turn (LP-49), so the Settings toggle applies without a restart.

    LEO_AUTOSAVE_DISABLED=1 is checked first: the test suite sets it because in the
    dev container /app/leonardo is the LIVE repo.
    """
    if os.getenv("LEO_AUTOSAVE_DISABLED") == "1":
        return True
    from app.services import checkpoint_service
    return not checkpoint_service.autosave_enabled()


def autosave_turn_start_hook() -> None:
    if _autosave_off():
        return
    from app.services import checkpoint_service
    checkpoint_service.note_turn_start()


def autosave_hook(request, thread_id):
    """End of turn: one commit of whatever the turn changed (LP-46)."""
    if _autosave_off():
        return None
    # A coding task is mid-edit; its results arrive as a turn of their own, which saves.
    if shared_run_manager().writers_busy():
        logger.info(f"Auto-save skipped on thread {thread_id}: a coding task is still editing")
        return None
    from app.services import checkpoint_service
    return checkpoint_service.autosave_after_turn(request, thread_id=thread_id)
