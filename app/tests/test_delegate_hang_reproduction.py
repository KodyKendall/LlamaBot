"""Controlled reproduction for the pre-fix blocking ``delegate_task`` hang."""

import asyncio
import multiprocessing
import queue
import threading
from types import SimpleNamespace


def _run_ticket_delegation(result_queue):
    """Run the real ticket-mode tool in an isolated, safely-killable process."""
    from app.agents.leonardo.rails_ticket_mode_agent import sub_agents

    class BlockingSubAgent:
        def invoke(self, *_args, **_kwargs):
            threading.Event().wait()

        async def ainvoke(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    sub_agents.create_sub_agent = lambda **_kwargs: BlockingSubAgent()

    # The fixed tool delegates timeout ownership to the shared runtime. Keep the
    # production behavior but shorten its constants so this regression stays fast.
    if hasattr(sub_agents, "run_delegation"):
        production_run_delegation = sub_agents.run_delegation

        async def short_run_delegation(*args, **kwargs):
            return await production_run_delegation(
                *args,
                timeout_seconds=0.05,
                heartbeat_seconds=0.01,
                **kwargs,
            )

        sub_agents.run_delegation = short_run_delegation

    runtime = SimpleNamespace(
        tool_call_id="call-repro",
        state={"llm_model": "deepseek-v4-flash"},
        config={"configurable": {"thread_id": "thread-repro"}},
        stream_writer=lambda event: result_queue.put({"event": event}),
    )
    result_queue.put({"ready": True})

    tool = sub_agents.delegate_task
    if tool.coroutine is not None:
        command = asyncio.run(
            tool.coroutine(task_description="controlled stall", runtime=runtime)
        )
    else:
        command = tool.func(task_description="controlled stall", runtime=runtime)

    message = command.update["messages"][0]
    result_queue.put({
        "result": {
            "content": message.content,
            "failed_tool_calls_count": command.update.get("failed_tool_calls_count"),
        }
    })


def test_ticket_delegate_task_recovers_from_a_blocked_sub_agent():
    """Pre-fix this process remains alive forever; fixed code exits via timeout."""
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    process = context.Process(target=_run_ticket_delegation, args=(result_queue,))
    process.start()
    assert result_queue.get(timeout=5) == {"ready": True}
    process.join(timeout=0.5)

    was_still_blocked = process.is_alive()
    if was_still_blocked:
        process.terminate()
        process.join(timeout=1)

    assert not was_still_blocked, (
        "delegate_task remained blocked after its deadline; this reproduces the "
        "pre-fix silent hang"
    )
    result = None
    while result is None:
        item = result_queue.get(timeout=1)
        result = item.get("result")
    assert "[DELEGATED TASK FAILED]" in result["content"]
    assert "Timed out" in result["content"]
    assert result["failed_tool_calls_count"] == 1


def test_ticket_delegate_task_emits_progress_before_completion():
    """Pre-fix the blocked tool emits no event; fixed code emits immediately."""
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    process = context.Process(target=_run_ticket_delegation, args=(result_queue,))
    process.start()
    assert result_queue.get(timeout=5) == {"ready": True}

    try:
        item = result_queue.get(timeout=0.2)
    except queue.Empty:
        item = None
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)

    assert item is not None, (
        "delegate_task emitted no progress while its nested agent was blocked; "
        "this reproduces the pre-fix silent UI"
    )
    assert item["event"]["type"] == "delegation_progress"
    assert item["event"]["phase"] == "started"
