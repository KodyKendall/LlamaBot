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
    """Raised after the whole nested-agent delegation exceeds its deadline."""


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
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await sub_agent.ainvoke(input_data, config=config)
        emit("completed", f"{label} completed")
        return result
    except TimeoutError as exc:
        elapsed = f"{timeout_seconds:g}"
        emit("timed_out", f"{label} timed out after {elapsed}s")
        logger.warning("%s timed out after %ss", label, elapsed)
        raise DelegationTimedOut(f"timed out after {elapsed}s") from exc
    except Exception:
        emit("failed", f"{label} failed")
        raise
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
