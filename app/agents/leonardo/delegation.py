"""Shared runtime for bounded, observable nested-agent delegation."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)

DELEGATION_TIMEOUT_SECONDS = 240
DELEGATION_HEARTBEAT_SECONDS = 15


class DelegationTimedOut(TimeoutError):
    """Raised after the whole nested-agent delegation exceeds its deadline.

    Carries ``partial_messages`` — everything the sub-agent had done when the
    deadline hit. The damage from a timeout was never the timeout: it was the
    silence. The sub-agent has already edited files, and the parent got no record
    of which, so it had to re-read the tree or (worse) retry and edit them twice.
    """

    def __init__(self, message: str = "", *, partial_messages=None):
        super().__init__(message)
        self.partial_messages = list(partial_messages or [])


def _tool_calls_of(message):
    return list(getattr(message, "tool_calls", None) or [])


def _tool_results_by_id(messages) -> dict:
    results = {}
    for msg in messages:
        call_id = getattr(msg, "tool_call_id", None)
        if call_id:
            results[call_id] = msg
    return results


#: Tools whose calls are worth reporting back, and the argument that names the
#: thing they acted on. Anything else is noise in a recovery report.
_REPORTABLE_TOOLS = {
    "write_file": "file_path",
    "edit_file": "file_path",
    "bash_command": "command",
    "write_leonardo_md": "path",
    "edit_leonardo_md": "path",
}

_ERROR_MARKERS = ("Error:", "error:", "rails aborted!", "did NOT persist",
                  "Could not find", "FAILED")


def summarize_partial_work(messages, *, max_items: int = 25) -> str:
    """What the sub-agent actually did, for a parent that has to recover from it.

    A partial report is recoverable; nothing is not. Reports the files written or
    edited, the commands run and whether they looked like they failed, and the
    step that was in flight when the deadline hit.
    """
    messages = list(messages or [])
    if not messages:
        return "No record of what the sub-agent did before the deadline."

    results = _tool_results_by_id(messages)
    files, commands = [], []
    in_flight = None

    for msg in messages:
        for call in _tool_calls_of(msg):
            name = call.get("name")
            arg_key = _REPORTABLE_TOOLS.get(name)
            if arg_key is None:
                continue
            value = (call.get("args") or {}).get(arg_key)
            if not value:
                continue
            result = results.get(call.get("id"))
            if result is None:
                # No ToolMessage answered it, so this is where it stopped.
                in_flight = f"{name}({value})"
                continue
            content = str(getattr(result, "content", "") or "")
            failed = any(marker in content for marker in _ERROR_MARKERS)
            if name in ("write_file", "edit_file"):
                files.append((str(value), failed))
            else:
                commands.append((str(value), failed))

    lines = []
    if files:
        lines.append("Files it wrote or edited:")
        for path, failed in files[:max_items]:
            lines.append(f"  - {path}{'  (the call reported an error)' if failed else ''}")
        if len(files) > max_items:
            lines.append(f"  - …and {len(files) - max_items} more")
    if commands:
        lines.append("Commands it ran:")
        for command, failed in commands[:max_items]:
            trimmed = command if len(command) <= 200 else command[:200] + "…"
            lines.append(f"  - {trimmed}{'  (failed)' if failed else ''}")
        if len(commands) > max_items:
            lines.append(f"  - …and {len(commands) - max_items} more")
    if in_flight:
        lines.append(f"In progress when it ran out of time: {in_flight}")

    if not lines:
        return "It had not written any files or run any commands yet."
    return "\n".join(lines)


async def run_delegation(
    sub_agent,
    input_data: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    stream_writer: Callable[[dict[str, Any]], None] | None = None,
    timeout_seconds: float = DELEGATION_TIMEOUT_SECONDS,
    heartbeat_seconds: float = DELEGATION_HEARTBEAT_SECONDS,
    label: str = "Sub-agent",
):
    """Run a nested agent with a whole-run deadline and progress heartbeats.

    ``ainvoke`` is intentional: cancelling an executor-wrapped synchronous invoke
    returns control to the caller but leaves the blocked worker (and HTTP request)
    alive. Async cancellation reaches the provider client instead.
    """
    started_at = time.monotonic()

    def emit(phase: str, message: str) -> None:
        if stream_writer is None:
            return
        stream_writer({
            "type": "delegation_progress",
            "phase": phase,
            "elapsed_seconds": round(time.monotonic() - started_at),
            "message": message,
        })

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            elapsed = round(time.monotonic() - started_at)
            emit("working", f"{label} is still working… ({elapsed}s)")

    emit("started", f"{label} started")
    heartbeat_task = asyncio.create_task(heartbeat())

    # Streamed rather than ainvoke'd purely so a timeout can say what happened.
    # `values` yields the whole state after each step, so whatever the last
    # completed step left behind is still in hand when the deadline fires.
    # (`astream` is as cancellable as `ainvoke` — cancellation still reaches the
    # provider client, which is why neither is run in an executor.)
    latest: dict = {}
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in sub_agent.astream(
                input_data, config=config, stream_mode="values"
            ):
                if isinstance(chunk, dict):
                    latest = chunk
        emit("completed", f"{label} completed")
        return latest
    except TimeoutError as exc:
        elapsed = f"{timeout_seconds:g}"
        emit("timed_out", f"{label} timed out after {elapsed}s")
        logger.warning("%s timed out after %ss", label, elapsed)
        raise DelegationTimedOut(
            f"timed out after {elapsed}s",
            partial_messages=latest.get("messages") or [],
        ) from exc
    except Exception:
        emit("failed", f"{label} failed")
        raise
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
