"""
Repair a corrupted LangGraph thread whose checkpointed message list violates
the LLM contract that every `tool` message must be preceded by an `assistant`
message bearing matching `tool_calls`.

Two corruption shapes are detected and fixable:

  (A) Orphan ToolMessage
      A ToolMessage with no preceding AIMessage whose tool_calls include its
      tool_call_id. Cause: prior repair / summarization dropped the anchoring
      AIMessage but left the ToolMessage behind. Fix: RemoveMessage it.

  (B) Dangling tool_calls
      An AIMessage with tool_calls that have no corresponding following
      ToolMessage. Cause: task cancelled mid-tool. Fix: inject a synthetic
      "[Cancelled]" ToolMessage for each missing tool_call_id (same logic as
      _repair_thread_state_if_needed in request_handler.py).

Run inside the llamabot container:

  docker compose -f docker-compose-dev.yml exec llamabot \\
      python -m app.scripts.repair_thread <thread_id> [--agent rails_agent] [--fix]

Without --fix the script only inspects and reports. With --fix it applies the
repair and re-reads state to verify.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.messages import ToolMessage as LCToolMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool


def _db_uri() -> str:
    uri = (
        os.getenv("CHECKPOINTER_DB_URI")
        or os.getenv("LEONARDO_DB_URI")
        or os.getenv("AUTH_DB_URI")
        or os.getenv("DB_URI")
    )
    if not uri or not uri.strip():
        sys.exit("No DB URI configured (CHECKPOINTER_DB_URI / LEONARDO_DB_URI / AUTH_DB_URI / DB_URI).")
    return uri


def _build_compiled_graph(agent_name: str, checkpointer):
    """Import the agent's build_workflow and compile it with our checkpointer.

    Mirrors how request_handler.get_app_from_workflow_string resolves agents
    via langgraph.json. We do not need the cached/compiled instance from app
    state because we only call aget_state / aupdate_state, which round-trip
    through the checkpointer regardless of node wiring.
    """
    import importlib
    import inspect

    from app.lib.langgraph_registry import load_graphs
    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "langgraph.json"))
    graphs = load_graphs(base_path)
    if agent_name not in graphs:
        sys.exit(f"Unknown agent '{agent_name}'. Known: {sorted(graphs)}")

    workflow_string = graphs[agent_name]
    module_path, function_name = workflow_string.split(":")
    if module_path.startswith("./"):
        module_path = module_path[2:]
    module_path = module_path.replace("/", ".").replace(".py", "")

    module = importlib.import_module(module_path)
    builder = getattr(module, function_name)

    sig = inspect.signature(builder)
    if "ask_before_edits" in sig.parameters:
        return builder(checkpointer=checkpointer, ask_before_edits=False)
    return builder(checkpointer=checkpointer)


def _summarize_message(i: int, msg: Any) -> str:
    kind = type(msg).__name__
    mid = getattr(msg, "id", None)
    extra = ""
    if isinstance(msg, AIMessage):
        tcs = getattr(msg, "tool_calls", []) or []
        if tcs:
            extra = f" tool_calls=[{', '.join(tc.get('id', '?') for tc in tcs)}]"
    elif isinstance(msg, LCToolMessage):
        extra = f" tool_call_id={getattr(msg, 'tool_call_id', None)}"
    elif isinstance(msg, HumanMessage):
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        extra = f" content={str(content)[:60]!r}"
    return f"  [{i:>3}] {kind} id={mid}{extra}"


def _scan(messages: list[Any]) -> tuple[list[tuple[int, Any]], list[tuple[int, Any, list[str]]]]:
    """Return (orphan_tool_messages, dangling_tool_calls).

    orphan_tool_messages: list of (index, ToolMessage) with no preceding
        AIMessage whose tool_calls include its tool_call_id.

    dangling_tool_calls: list of (index, AIMessage, missing_ids) for any
        AIMessage whose tool_calls are not all satisfied by following
        ToolMessages before the next AIMessage.
    """
    orphans: list[tuple[int, Any]] = []
    dangling: list[tuple[int, Any, list[str]]] = []

    # Track the "live" set of tool_call_ids that have an open anchoring AIMessage.
    # When we see a ToolMessage, we consume the matching id. When we hit a new
    # AIMessage we close out the previous anchor.
    open_ids: set[str] = set()

    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            # Close out previous anchor: anything still in open_ids was a
            # dangling tool_call — but we've already recorded those when we
            # initially saw the AIMessage. Just reset.
            open_ids = {tc["id"] for tc in (getattr(msg, "tool_calls", []) or [])}
        elif isinstance(msg, LCToolMessage):
            tcid = getattr(msg, "tool_call_id", None)
            if tcid in open_ids:
                open_ids.discard(tcid)
            else:
                orphans.append((i, msg))

    # Second pass for dangling tool_calls — easier to scan independently.
    for i, msg in enumerate(messages):
        if not isinstance(msg, AIMessage):
            continue
        tcs = getattr(msg, "tool_calls", []) or []
        if not tcs:
            continue
        expected = {tc["id"] for tc in tcs}
        found: set[str] = set()
        for j in range(i + 1, len(messages)):
            nxt = messages[j]
            if isinstance(nxt, LCToolMessage):
                found.add(getattr(nxt, "tool_call_id", None))
            elif isinstance(nxt, AIMessage):
                break
        missing = sorted(expected - found)
        if missing:
            dangling.append((i, msg, missing))

    return orphans, dangling


async def _run(thread_id: str, agent_name: str, apply_fix: bool) -> int:
    db_uri = _db_uri()
    pool = AsyncConnectionPool(db_uri, min_size=1, max_size=2, timeout=5.0, open=False)
    await pool.open()
    try:
        checkpointer = AsyncPostgresSaver(pool)
        graph = _build_compiled_graph(agent_name, checkpointer)
        config = {"configurable": {"thread_id": str(thread_id)}}

        snapshot = await graph.aget_state(config)
        if not snapshot or not snapshot.values:
            print(f"No state found for thread_id={thread_id} (agent={agent_name}).")
            return 1
        messages = snapshot.values.get("messages", [])
        print(f"Thread {thread_id} has {len(messages)} messages.\n")
        for i, m in enumerate(messages):
            print(_summarize_message(i, m))
        print()

        orphans, dangling = _scan(messages)
        print(f"Orphan ToolMessages:    {len(orphans)}")
        for i, m in orphans:
            print(f"  - index {i}, tool_call_id={getattr(m, 'tool_call_id', None)}, id={getattr(m, 'id', None)}")
        print(f"Dangling tool_calls:    {len(dangling)}")
        for i, m, missing in dangling:
            print(f"  - index {i}, AIMessage id={getattr(m, 'id', None)}, missing tool_call_ids={missing}")

        if not orphans and not dangling:
            print("\nState is clean. Nothing to repair.")
            return 0

        if not apply_fix:
            print("\nDry run (no changes made). Re-run with --fix to apply the repair.")
            return 0

        # Apply fix
        ops: list[Any] = []
        for _, m in orphans:
            mid = getattr(m, "id", None)
            if mid:
                ops.append(RemoveMessage(id=mid))
            else:
                print(f"  ! orphan ToolMessage at unknown id — cannot remove. Skipping.")
        for _, ai_msg, missing in dangling:
            for tcid in missing:
                # Find tool name from the AIMessage's tool_calls (best-effort)
                name = "unknown"
                for tc in getattr(ai_msg, "tool_calls", []) or []:
                    if tc.get("id") == tcid:
                        name = tc.get("name", "unknown")
                        break
                ops.append(
                    LCToolMessage(
                        content="[Cancelled] Tool execution was interrupted before completion.",
                        tool_call_id=tcid,
                        name=name,
                    )
                )

        print(f"\nApplying {len(ops)} repair op(s)...")
        await graph.aupdate_state(config, {"messages": ops})
        print("Applied. Re-reading state to verify...\n")

        snapshot2 = await graph.aget_state(config)
        messages2 = snapshot2.values.get("messages", []) if snapshot2 and snapshot2.values else []
        orphans2, dangling2 = _scan(messages2)
        print(f"Post-repair: {len(messages2)} messages, orphans={len(orphans2)}, dangling={len(dangling2)}.")
        if orphans2 or dangling2:
            print("Repair did NOT fully clean the thread. Inspect manually:")
            for i, m in enumerate(messages2):
                print(_summarize_message(i, m))
            return 2
        print("Structural invariants OK — every ToolMessage is anchored, every tool_call has a response.")
        return 0
    finally:
        await pool.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("thread_id", help="LangGraph thread_id to inspect/repair")
    p.add_argument("--agent", default="rails_agent", help="Agent name from langgraph.json (default: rails_agent)")
    p.add_argument("--fix", action="store_true", help="Apply the repair. Without this flag the script only inspects.")
    args = p.parse_args()

    rc = asyncio.run(_run(args.thread_id, args.agent, args.fix))
    sys.exit(rc)


if __name__ == "__main__":
    main()
