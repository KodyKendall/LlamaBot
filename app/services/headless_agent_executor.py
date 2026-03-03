"""
Headless Agent Executor for scheduled jobs.

Executes LangGraph agents without WebSocket connection, collecting
results for storage in ScheduledJobRun records.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def execute_agent_headless(
    agent_name: str,
    prompt: str,
    llm_model: str,
    job_id: int,
    trigger_type: str,
    max_duration_seconds: int,
    recursion_limit: int,
    app: "FastAPI",
    triggered_by_user_id: Optional[int] = None
):
    """
    Execute an agent workflow without WebSocket, collecting results.

    This mirrors the logic in RequestHandler.handle_request() but:
    1. Does not stream to WebSocket
    2. Collects output and token usage
    3. Stores results in ScheduledJobRun

    Args:
        agent_name: Name of agent from langgraph.json (e.g., "rails_agent")
        prompt: The instruction/prompt for the agent
        llm_model: Model to use (e.g., "claude-4.5-haiku")
        job_id: ScheduledJob.id for linking the run
        trigger_type: "cron" | "manual" | "api"
        max_duration_seconds: Timeout for execution
        recursion_limit: LangGraph recursion limit
        app: FastAPI app instance
        triggered_by_user_id: Optional user who triggered (for manual runs)

    Returns:
        ScheduledJobRun record with results
    """
    from sqlmodel import Session
    from app.db import engine
    from app.models import ScheduledJobRun

    # Generate unique thread_id for this run
    thread_id = f"scheduled-{job_id}-{uuid.uuid4().hex[:8]}"

    # Create run record
    run = ScheduledJobRun(
        job_id=job_id,
        status="running",
        trigger_type=trigger_type,
        thread_id=thread_id,
        started_at=datetime.now(timezone.utc),
        triggered_by_user_id=triggered_by_user_id
    )

    with Session(engine) as session:
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    logger.info(f"Starting headless execution: job_id={job_id}, thread_id={thread_id}, agent={agent_name}")

    # Get compiled graph from cache
    graph = None
    if hasattr(app.state, 'compiled_graphs'):
        graph = app.state.compiled_graphs.get(agent_name)

    if not graph:
        logger.error(f"Agent '{agent_name}' not found in compiled_graphs")
        return _mark_run_failed(run_id, f"Agent '{agent_name}' not found")

    # Build state (similar to RequestHandler.get_langgraph_app_and_state)
    state = {
        "messages": [HumanMessage(content=prompt)],
        "llm_model": llm_model,
        "agent_prompt": "",  # Can be customized per-job if needed
        "origin": "scheduled",
    }

    config = {
        "configurable": {
            "thread_id": thread_id,
            "origin": "scheduled",
            "recursion_limit": recursion_limit
        },
        "recursion_limit": recursion_limit
    }

    # Execute with timeout
    output_messages = []
    total_input_tokens = 0
    total_output_tokens = 0

    try:
        async with asyncio.timeout(max_duration_seconds):
            async for chunk in graph.astream(
                state,
                config=config,
                stream_mode=["updates", "messages"],
                subgraphs=True
            ):
                # Process chunks like in request_handler.py
                is_update = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'updates'

                if is_update:
                    state_object = chunk[2]
                    for agent_key, agent_data in state_object.items():
                        if isinstance(agent_data, dict) and 'messages' in agent_data:
                            messages = agent_data['messages']
                            if messages:
                                message = messages[-1]
                                # Collect output
                                if hasattr(message, 'content'):
                                    content = message.content
                                    if isinstance(content, str):
                                        output_messages.append(content)
                                    elif isinstance(content, list):
                                        # Handle content blocks (extract text)
                                        text_parts = []
                                        for block in content:
                                            if isinstance(block, dict) and block.get("type") == "text":
                                                text_parts.append(block.get("text", ""))
                                            elif isinstance(block, str):
                                                text_parts.append(block)
                                        if text_parts:
                                            output_messages.append("".join(text_parts))

                                # Collect token usage
                                usage = getattr(message, 'usage_metadata', None)
                                if usage:
                                    if isinstance(usage, dict):
                                        total_input_tokens += usage.get('input_tokens', 0)
                                        total_output_tokens += usage.get('output_tokens', 0)
                                    else:
                                        total_input_tokens += getattr(usage, 'input_tokens', 0)
                                        total_output_tokens += getattr(usage, 'output_tokens', 0)

        # Success - update run record
        # Take last few messages as summary (truncate to 5000 chars)
        output_summary = "\n\n---\n\n".join(output_messages[-3:])[:5000] if output_messages else ""

        logger.info(f"Headless execution completed: job_id={job_id}, tokens={total_input_tokens + total_output_tokens}")

        return _mark_run_completed(
            run_id,
            output_summary=output_summary,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens
        )

    except asyncio.TimeoutError:
        logger.warning(f"Headless execution timeout: job_id={job_id}, timeout={max_duration_seconds}s")
        return _mark_run_failed(run_id, f"Execution timeout after {max_duration_seconds}s", status="timeout")
    except Exception as e:
        logger.error(f"Headless execution failed: job_id={job_id}, error={e}", exc_info=True)
        return _mark_run_failed(run_id, str(e)[:2000])


def _mark_run_completed(run_id: int, output_summary: str, input_tokens: int, output_tokens: int):
    """Update run record as completed."""
    from sqlmodel import Session
    from app.db import engine
    from app.models import ScheduledJobRun

    with Session(engine) as session:
        run = session.get(ScheduledJobRun, run_id)
        if run:
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
            # Handle timezone-naive started_at from database
            if run.started_at:
                started = run.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                run.duration_seconds = (run.completed_at - started).total_seconds()
            else:
                run.duration_seconds = 0
            run.output_summary = output_summary
            run.input_tokens = input_tokens
            run.output_tokens = output_tokens
            run.total_tokens = input_tokens + output_tokens
            session.add(run)
            session.commit()
            session.refresh(run)
            return run
    return None


def _mark_run_failed(run_id: int, error_message: str, status: str = "failed"):
    """Update run record as failed."""
    from sqlmodel import Session
    from app.db import engine
    from app.models import ScheduledJobRun

    with Session(engine) as session:
        run = session.get(ScheduledJobRun, run_id)
        if run:
            run.status = status
            run.completed_at = datetime.now(timezone.utc)
            # Handle timezone-naive started_at from database
            if run.started_at:
                started = run.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                run.duration_seconds = (run.completed_at - started).total_seconds()
            else:
                run.duration_seconds = 0
            run.error_message = error_message
            session.add(run)
            session.commit()
            session.refresh(run)
            return run
    return None
