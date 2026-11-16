"""The builder as an MCP server: ``POST /mcp`` (LP-50).

Lets the mothership (Leo at llamapress.ai/ai, the phone app) and outside AIs
(Claude, ChatGPT) drive this box's builder — send a message, watch the run,
answer an approval or question, stop it, read threads.

Nothing here runs the agent itself. A message goes through the SAME path as a
``/ws`` chat frame: ``RequestHandler`` submits it to the app-wide
``RunManager`` (a message to a thread mid-run is QUEUED, exactly like the
browser), and the run writes the same frames to the same per-thread log the
browser attaches to. ``get_run`` reads that log. So:

* an MCP run shows up in the box's own chat history and live in an open tab;
* the run keeps going if the MCP caller disconnects;
* the paywall, the instance lock, ZDR, tool limits and HITL approvals apply
  unchanged, because they live inside ``handle_request``.

``/ws`` is not touched.

Transport: MCP Streamable HTTP, stateless, JSON responses (``mcp`` 1.9.2). Each
POST gets its own short-lived transport, so no session state and no lifespan
hook. ``GET`` (the optional server-to-client SSE stream) answers 405, which the
spec allows. Auth: bearer only — see app/services/mcp_auth.py and
docs/dev/builder_mcp_server.md. No OAuth; ``/.well-known/oauth-protected-resource``
exists only so the 401 can point at it (RFC 9728) and says "bearer header".
"""
import asyncio
import json
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Optional

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.services import mcp_auth

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource"
#: JSON-RPC methods answered without a token (they reveal nothing and run nothing).
PUBLIC_METHODS = frozenset({"initialize", "notifications/initialized", "ping"})
MAX_BODY_BYTES = 1_000_000

#: Keep every tool result under ~12k characters; page the rest with cursors.
RESULT_BUDGET = 11_000
TEXT_CAP = 2_500
DETAIL_CAP = 300
PENDING_CAP = 1_500
GET_RUN_MAX_WAIT = 25
SEND_MAX_WAIT = 60
POLL_INTERVAL = 0.2

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITES = ToolAnnotations(readOnlyHint=False)

#: Frames that mean the run is paused on the person, by what they are waiting for.
APPROVAL_FRAMES = frozenset({"approval_request"})
QUESTION_FRAMES = frozenset({
    "question_request", "uiux_question_request", "browser_command",
    "suggest_mode_switch", "implement_ticket",
})
FILE_TOOLS = frozenset({"edit_file", "write_file", "multi_edit", "create_file", "delete_file"})
NOTICE_FRAMES = frozenset({
    "system_message", "queued", "model_substituted", "paywall_hit", "instance_locked",
})

mcp = FastMCP(
    "LlamaBot builder",
    instructions=(
        "Drive this Leo box's builder (the agent that edits the app). "
        "send_message starts or queues a run; poll get_run until status is done, "
        "error or cancelled; when it is waiting_for_approval or waiting_for_answer, "
        "call answer. Runs show up in the box's own chat."
    ),
    stateless_http=True,
    json_response=True,
)


# =========================================================================== seams
# Small functions so tests can stand in for the database and permissions.

def _custom_modes() -> dict:
    from app.permissions import custom_modes
    return custom_modes()


def _is_admin(principal: dict) -> bool:
    return bool(principal.get("is_admin") or principal.get("is_system_admin"))


def _allowed_modes(principal: dict, custom: dict) -> list:
    """Mode keys this caller may run — the same resolver the /ws gate uses."""
    from sqlmodel import Session

    from app.db import engine
    from app.permissions import allowed_modes

    with Session(engine) as session:
        return allowed_modes(session, principal.get("role"), _is_admin(principal), custom)


def _thread_allowed(principal: dict, thread_id: str) -> bool:
    """Same rule as WebSocketHandler._authorize_thread. Fails closed."""
    user_id = principal.get("user_id")
    if user_id is None:
        return True  # the gem / no LlamaBot account: owns nothing, same as /ws
    from sqlmodel import Session

    from app.db import engine
    from app.services.thread_service import can_access_thread

    if engine is None:
        return True
    try:
        with Session(engine) as session:
            return can_access_thread(session, str(thread_id), user_id, bool(principal.get("is_admin")))
    except Exception as e:  # noqa: BLE001
        logger.error(f"MCP: thread ownership check failed for {thread_id}, denying: {e}")
        return False


def _thread_rows(principal: dict, limit: int) -> list:
    from sqlmodel import Session

    from app.db import engine
    from app.services.thread_service import get_thread_list

    if engine is None:
        return []
    with Session(engine) as session:
        rows = get_thread_list(
            session, user_id=principal.get("user_id"),
            include_unowned=bool(principal.get("is_admin")), limit=limit,
        )
        return [{
            "thread_id": t.thread_id,
            "title": t.title,
            "updated_at": t.updated_at.isoformat() + "Z",
            "message_count": t.message_count,
            "agent_name": t.agent_name,
        } for t in rows]


# =========================================================================== helpers

def _principal(ctx: Context) -> dict:
    request = ctx.request_context.request
    principal = getattr(getattr(request, "state", None), "mcp_principal", None)
    if not principal:
        raise ToolError("Not authenticated.")
    # The switch is checked per request in the endpoint; checked again here so a
    # push that lands mid-request still stops a run from starting.
    if not mcp_auth.is_enabled():
        raise ToolError("MCP is switched off on this box.")
    return principal


def _app(ctx: Context):
    return ctx.request_context.request.app


def _request_handler(app):
    handler = getattr(app.state, "mcp_request_handler", None)
    if handler is None:
        from app.websocket.request_handler import RequestHandler
        handler = RequestHandler(app)
        app.state.mcp_request_handler = handler
    return handler


def _run_manager(app):
    return _request_handler(app)._run_manager()


def _stamp_turn_owner(principal: dict) -> None:
    """What WebSocketHandler._handle_auth_message stamps for a socket's turns.

    The run task copies this context, so the turn runs as this user: their
    ChatGPT plan, their tool grant, their name on mothership reports, and the new
    thread is saved as theirs (it shows in their chat history).
    """
    from app.lib.request_context import set_current_principal, set_current_user_id
    from app.services import user_context

    set_current_user_id(principal.get("user_id"))
    all_tools = _is_admin(principal)
    set_current_principal(
        principal.get("role"), all_tools,
        security_only=bool(principal.get("is_security_admin")) and not all_tools,
    )
    try:
        user_context.set_current(
            user_context.for_user_id(principal.get("user_id"))
            or user_context.from_token_payload(principal)
        )
    except Exception:  # noqa: BLE001 — attribution must not block a run
        logger.debug("MCP: user context not stamped", exc_info=True)


def _base_agent(mode: str, custom: dict) -> Optional[str]:
    """The graph a mode key runs (its non-plan variant), as config.js maps it."""
    from app.permissions import MODE_AGENTS

    if mode in custom:
        return custom[mode]
    graphs = sorted(g for g in MODE_AGENTS.get(mode, ()) if "plan_mode" not in g)
    return graphs[0] if graphs else None


def _allowed_agent_names(principal: dict, custom: dict) -> set:
    from app.permissions import MODE_AGENTS

    names = set()
    for mode in _allowed_modes(principal, custom):
        names |= set(MODE_AGENTS.get(mode, ()))
        if mode in custom:
            names.add(custom[mode])
    return names


def _check_thread(principal: dict, thread_id: str) -> str:
    thread_id = str(thread_id or "").strip()
    if not thread_id or len(thread_id) > 200:
        raise ToolError("Invalid thread_id.")
    if not _thread_allowed(principal, thread_id):
        raise ToolError("Chat not found.")
    return thread_id


def _parse_run_id(principal: dict, run_id: str) -> tuple:
    thread_id, sep, seq = str(run_id or "").rpartition("@")
    if not sep or not seq.isdigit():
        raise ToolError("Invalid run_id.")
    return _check_thread(principal, thread_id), int(seq)


def _run_id(thread_id: str, seq: int) -> str:
    return f"{thread_id}@{seq}"


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in ("text", "text_delta") and not block.get("thought"):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def _cap(text, limit: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_calls(frame: dict) -> list:
    """Normalised ``[(name, args)]`` from an ``ai`` frame (either wire shape)."""
    calls = (frame.get("base_message") or {}).get("tool_calls") or frame.get("tool_calls") or []
    out = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        args = call.get("args")
        fn = call.get("function")
        if not name and isinstance(fn, dict):
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = fn.get("arguments")
        if name:
            out.append((name, args if args is not None else {}))
    return out


def _steps_for(frame: dict) -> list:
    """One frame the browser would render → zero or more short text steps."""
    kind = frame.get("type")
    steps = []
    if kind == "ai":
        for name, args in _tool_calls(frame):
            if name == "write_todos":
                todos = (args or {}).get("todos") or []
                detail = "; ".join(f"[{t.get('status', '')}] {t.get('content', '')}"
                                   for t in todos if isinstance(t, dict))
                steps.append({"type": "todo_update", "title": "Updated the plan", "detail": detail})
            elif name in FILE_TOOLS:
                path = (args or {}).get("file_path") or (args or {}).get("path") or ""
                steps.append({"type": "file_edit", "title": name, "detail": str(path)})
            else:
                steps.append({"type": "tool_call", "title": name, "detail": json.dumps(args, default=str)})
        text = _text(frame.get("content"))
        if text.strip():
            who = "sub-agent" if frame.get("is_subagent") else "Leo"
            steps.append({"type": "message", "title": who, "detail": text})
    elif kind == "tool":
        name = (frame.get("base_message") or {}).get("name") or "tool"
        steps.append({"type": "tool_result", "title": name, "detail": _text(frame.get("content"))})
    elif kind in APPROVAL_FRAMES:
        names = ", ".join(a.get("name", "") for a in frame.get("action_requests") or [])
        steps.append({"type": "approval_request", "title": "Needs approval", "detail": names})
    elif kind in QUESTION_FRAMES:
        question = frame.get("question") or frame.get("reason") or frame.get("command") or kind
        steps.append({"type": "question", "title": "Needs an answer", "detail": _text(question)})
    elif kind == "error":
        steps.append({"type": "error", "title": "Error", "detail": _text(frame.get("content"))})
    elif kind in NOTICE_FRAMES:
        steps.append({"type": "notice", "title": kind, "detail": _text(frame.get("content"))})
    elif kind == "background_task":
        steps.append({"type": "background_task", "title": frame.get("label") or frame.get("kind") or "task",
                      "detail": str(frame.get("status", ""))})
    for step in steps:
        step["detail"] = _cap(step["detail"], DETAIL_CAP)
    return steps


def _run_state(handle, start_seq: int) -> dict:
    """``status``, the builder's latest text, and what it is waiting on (if anything)."""
    events = handle.log.since(start_seq)
    text = ""
    paused_on = None
    for frame in events:
        kind = frame.get("type")
        if kind == "run_started":
            paused_on = None
        elif kind in APPROVAL_FRAMES or kind in QUESTION_FRAMES:
            paused_on = frame
        elif kind == "ai" and not frame.get("is_subagent"):
            t = _text(frame.get("content"))
            if t.strip():
                text = t

    if handle.is_running() or any(not i.held for i in handle.pending):
        status = "running"
    elif handle.log.status in ("cancelled", "error"):
        status = handle.log.status
    elif paused_on is not None:
        status = "waiting_for_approval" if paused_on.get("type") in APPROVAL_FRAMES else "waiting_for_answer"
    elif events and any(f.get("type") == "error" for f in events[-3:]):
        status = "error"
    else:
        status = "done"

    pending = None
    if status.startswith("waiting") and paused_on is not None:
        pending = {k: v for k, v in paused_on.items() if k not in ("seq",)}
    return {"status": status, "text": text, "paused_on": paused_on, "pending": pending}


async def _wait(handle, start_seq: int, cursor: int, seconds: float) -> None:
    """Long-poll: return once there are new STEPS after ``cursor`` or the run stops
    running, or time runs out. Bookkeeping frames (run_started, token chunks) do
    not count — they woke the poll instantly in the first live smoke."""
    deadline = time.monotonic() + max(0.0, seconds)
    seen = cursor
    while time.monotonic() < deadline:
        if _run_state(handle, start_seq)["status"] != "running":
            return
        for frame in handle.log.since(seen):
            if _steps_for(frame):
                return
            seen = frame["seq"]
        await asyncio.sleep(POLL_INTERVAL)


async def _wait_until_settled(handle, start_seq: int, seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if _run_state(handle, start_seq)["status"] != "running":
            return
        await asyncio.sleep(POLL_INTERVAL)


@contextmanager
def _audit(tool: str, principal: Optional[dict] = None):
    """One INFO line per tool call: tool, who, thread, run, outcome, ms. Never message text."""
    record = {"thread_id": None, "run_id": None}
    started = time.monotonic()
    outcome = "ok"
    try:
        yield record
    except ToolError as e:
        outcome = f"refused:{_cap(str(e), 60)}"
        raise
    except Exception as e:
        outcome = f"error:{type(e).__name__}"
        raise
    finally:
        logger.info(
            "MCP tool=%s principal=%s thread=%s run=%s outcome=%s ms=%d",
            tool, (principal or {}).get("sub"), record["thread_id"], record["run_id"],
            outcome, (time.monotonic() - started) * 1000,
        )


# =========================================================================== tools

@mcp.tool(annotations=WRITES)
async def send_message(
    ctx: Context,
    message: str,
    thread_id: Optional[str] = None,
    agent_mode: str = "engineer",
    llm_model: Optional[str] = None,
    ask_before_edits: bool = False,
    wait_seconds: int = 0,
) -> dict:
    """Send the builder a message, exactly like typing it in the box's chat.

    Starts a run on the thread, or QUEUES the message if the thread is already
    running (the running agent reads it before its next step). Omit thread_id
    to start a new chat. agent_mode is a key from list_agent_modes. With
    ask_before_edits the run pauses for approval before editing files.
    wait_seconds (0-60) waits for the run to finish before answering.
    Returns {thread_id, run_id, status, text_so_far}; poll get_run(run_id).
    """
    principal = _principal(ctx)
    with _audit("send_message", principal) as audit:
        if not isinstance(message, str) or not message.strip():
            raise ToolError("message is required.")
        thread_id = _check_thread(principal, thread_id) if thread_id else str(uuid.uuid4())
        audit["thread_id"] = thread_id

        custom = _custom_modes()
        if agent_mode not in _allowed_modes(principal, custom):
            raise ToolError("You don't have access to that mode.")
        agent_name = _base_agent(agent_mode, custom)
        if not agent_name:
            raise ToolError("Unknown agent_mode.")

        frame = {
            "message": message,
            "thread_id": thread_id,
            "agent_name": agent_name,
            "agent_mode": agent_mode,
            "ask_before_edits": bool(ask_before_edits),
            "origin": "mcp",
        }
        if llm_model:
            frame["llm_model"] = llm_model

        app = _app(ctx)
        rm = _run_manager(app)
        existing = rm.get(thread_id)
        start_seq = existing.log.last_seq if existing is not None else 0
        run_id = _run_id(thread_id, start_seq)
        audit["run_id"] = run_id

        _stamp_turn_owner(principal)
        from app.websocket.run_manager import QueuedMessage
        handler = _request_handler(app)
        submitted = await rm.submit(thread_id, QueuedMessage(frame), handler._queued_run_factory)

        handle = rm.get(thread_id)
        wait = min(max(int(wait_seconds or 0), 0), SEND_MAX_WAIT)
        if wait:
            await _wait_until_settled(handle, start_seq, wait)
        state = _run_state(handle, start_seq)
        status = "queued" if submitted == "queued" and not wait else state["status"]
        return {
            "thread_id": thread_id,
            "run_id": run_id,
            "status": status,
            "text_so_far": _cap(state["text"], TEXT_CAP),
        }


@mcp.tool(annotations=READ_ONLY)
async def get_run(
    ctx: Context,
    run_id: str,
    after_event: Optional[int] = None,
    wait_seconds: int = GET_RUN_MAX_WAIT,
) -> dict:
    """What a run is doing. Long-polls up to wait_seconds (max 25): without
    after_event it waits for the run to stop running; with after_event it returns
    as soon as there are newer steps.

    status: running | waiting_for_approval | waiting_for_answer | done | error | cancelled.
    steps are what the box's chat shows (tool calls, file edits, plan updates),
    flattened to short text. Pass events_cursor back as after_event to get only
    newer steps; more=true means call again with that cursor right away.
    pending holds the approval request or question when the run is waiting.
    """
    principal = _principal(ctx)
    with _audit("get_run", principal) as audit:
        thread_id, start_seq = _parse_run_id(principal, run_id)
        audit["thread_id"], audit["run_id"] = thread_id, run_id
        handle = _run_manager(_app(ctx)).get(thread_id)
        if handle is None:
            raise ToolError("Unknown run (finished long ago or the box restarted). Use get_thread.")

        cursor = max(int(after_event if after_event is not None else start_seq), start_seq)
        wait = min(max(int(wait_seconds or 0), 0), GET_RUN_MAX_WAIT)
        if wait and after_event is None:
            # No cursor: the caller wants the outcome, so wait for the run to settle.
            await _wait_until_settled(handle, start_seq, wait)
        elif wait:
            await _wait(handle, start_seq, cursor, wait)

        state = _run_state(handle, start_seq)
        result = {
            "run_id": run_id,
            "thread_id": thread_id,
            "status": state["status"],
            "text": _cap(state["text"], TEXT_CAP),
            "steps": [],
            "events_cursor": cursor,
            "more": False,
        }
        if state["pending"] is not None:
            pending = state["pending"]
            if len(json.dumps(pending, default=str)) > PENDING_CAP:
                pending = {"type": pending.get("type"), "summary": _cap(json.dumps(pending, default=str), PENDING_CAP)}
            result["pending"] = pending

        used = len(json.dumps(result, default=str))
        for frame in handle.log.since(cursor):
            steps = _steps_for(frame)
            size = len(json.dumps(steps, default=str))
            if steps and used + size > RESULT_BUDGET:
                result["more"] = True
                break
            result["steps"].extend(steps)
            used += size
            result["events_cursor"] = frame["seq"]
        return result


@mcp.tool(annotations=WRITES)
async def answer(
    ctx: Context,
    run_id: str,
    approval: Optional[str] = None,
    answer: Optional[str] = None,
) -> dict:
    """Continue a run that is waiting on a person.

    waiting_for_approval: approval="approve" or "reject" (answer, if given, is
    the reason sent with a reject). waiting_for_answer: answer="<text>".
    Returns {run_id, status} — the run_id of the continued run; poll it.
    """
    principal = _principal(ctx)
    with _audit("answer", principal) as audit:
        thread_id, start_seq = _parse_run_id(principal, run_id)
        audit["thread_id"] = thread_id
        app = _app(ctx)
        rm = _run_manager(app)
        handle = rm.get(thread_id)
        if handle is None:
            raise ToolError("Unknown run.")
        state = _run_state(handle, start_seq)
        paused_on = state["paused_on"]
        agent_name = (paused_on or {}).get("agent_name")

        if state["status"] == "waiting_for_approval":
            if approval not in ("approve", "reject"):
                raise ToolError('This run is waiting for approval: pass approval="approve" or "reject".')
            count = max(1, len(paused_on.get("action_requests") or []))
            decision = {"type": "approve"} if approval == "approve" else {
                "type": "reject", "message": answer or "Rejected by the person reviewing this edit."}
            frame = {"type": "approval_response", "decisions": [dict(decision) for _ in range(count)],
                     "thread_id": thread_id, "agent_name": agent_name}
            kind = "approval"
        elif state["status"] == "waiting_for_answer":
            if not isinstance(answer, str) or not answer.strip():
                raise ToolError("This run is waiting for an answer: pass answer.")
            frame = {"type": "question_response", "answer": answer,
                     "thread_id": thread_id, "agent_name": agent_name}
            kind = "question"
        else:
            raise ToolError(f"Nothing to answer: the run is {state['status']}.")

        # The /ws gate checks every frame naming an agent, resumes included.
        if agent_name and agent_name not in _allowed_agent_names(principal, _custom_modes()):
            raise ToolError("You don't have access to that mode.")

        new_start = handle.log.last_seq
        new_run_id = _run_id(thread_id, new_start)
        audit["run_id"] = new_run_id
        _stamp_turn_owner(principal)
        await _request_handler(app).start_resume_run(frame, None, kind)
        return {"run_id": new_run_id, "thread_id": thread_id,
                "status": _run_state(rm.get(thread_id), new_start)["status"]}


@mcp.tool(annotations=WRITES)
async def cancel_run(ctx: Context, run_id: str) -> dict:
    """Stop the run (the chat's Stop button): the turn, anything queued, and its background tasks."""
    principal = _principal(ctx)
    with _audit("cancel_run", principal) as audit:
        thread_id, _ = _parse_run_id(principal, run_id)
        audit["thread_id"], audit["run_id"] = thread_id, run_id
        cancelled = await _run_manager(_app(ctx)).cancel(thread_id)
        return {"run_id": run_id, "cancelled": bool(cancelled)}


@mcp.tool(annotations=READ_ONLY)
async def list_threads(ctx: Context, limit: int = 20) -> dict:
    """Recent chats on this box that the caller may see, newest first (max 50)."""
    principal = _principal(ctx)
    with _audit("list_threads", principal):
        limit = min(max(int(limit or 20), 1), 50)
        return {"threads": _thread_rows(principal, limit)}


@mcp.tool(annotations=READ_ONLY)
async def get_thread(ctx: Context, thread_id: str, limit: int = 20) -> dict:
    """The last `limit` messages of a chat as text: [{role, text}] (role: human | ai | tool)."""
    principal = _principal(ctx)
    with _audit("get_thread", principal) as audit:
        thread_id = _check_thread(principal, thread_id)
        audit["thread_id"] = thread_id
        limit = min(max(int(limit or 20), 1), 100)
        values = await _request_handler(_app(ctx)).get_chat_history(thread_id) or {}
        if not isinstance(values, dict):
            values = getattr(values, "values", None) or {}
        messages = []
        for msg in (values.get("messages") or [])[-limit:]:
            text = _text(getattr(msg, "content", msg))
            if not text.strip():
                continue
            messages.append({"role": getattr(msg, "type", "unknown"), "text": _cap(text, 1_500)})
        while messages and len(json.dumps(messages)) > RESULT_BUDGET:
            messages.pop(0)
        return {"thread_id": thread_id, "messages": messages}


@mcp.tool(annotations=READ_ONLY)
async def list_agent_modes(ctx: Context) -> dict:
    """The agent modes this caller may use on this box: [{key, agent_name}]. Pass key as agent_mode."""
    principal = _principal(ctx)
    with _audit("list_agent_modes", principal):
        custom = _custom_modes()
        modes = []
        for key in _allowed_modes(principal, custom):
            agent = _base_agent(key, custom)
            if agent:
                modes.append({"key": key, "agent_name": agent})
        return {"modes": modes}


# =========================================================================== HTTP

def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _base_url(scope) -> str:
    proto = _header(scope, b"x-forwarded-proto").split(",")[0].strip() or scope.get("scheme", "https")
    host = _header(scope, b"x-forwarded-host").split(",")[0].strip() or _header(scope, b"host") or "localhost"
    return f"{proto}://{host}"


def _unauthorized(scope, description: str) -> Response:
    metadata = f"{_base_url(scope)}{METADATA_PATH}"
    return JSONResponse(
        {"error": "invalid_token", "error_description": description},
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
    )


def _bearer(scope) -> Optional[str]:
    value = _header(scope, b"authorization")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def _read_body(receive) -> Optional[bytes]:
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return b""
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


def _jsonrpc_methods(body: bytes) -> list:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return [None]
    items = payload if isinstance(payload, list) else [payload]
    return [i.get("method") if isinstance(i, dict) else None for i in items] or [None]


class McpEndpoint:
    """ASGI app for ``/mcp``: switch → auth → rate limit → MCP transport."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        if not mcp_auth.is_enabled():
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return

        token = _bearer(scope)
        principal = mcp_auth.authenticate(token) if token else None
        if token and principal is None:
            await _unauthorized(scope, "Invalid or expired token")(scope, receive, send)
            return

        body = b""
        public = False
        if scope["method"] == "POST":
            body = await _read_body(receive)
            if body is None:
                return
            if body == b"":
                await JSONResponse({"detail": "Request too large or empty"}, status_code=413)(scope, receive, send)
                return
            public = all(m in PUBLIC_METHODS for m in _jsonrpc_methods(body))

        if principal is None and not public:
            await _unauthorized(scope, "Bearer token required")(scope, receive, send)
            return
        if principal is not None and not mcp_auth.allow_request(token):
            await JSONResponse({"detail": "Rate limit exceeded"}, status_code=429,
                               headers={"Retry-After": "60"})(scope, receive, send)
            return
        if scope["method"] != "POST":
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return

        scope = dict(scope)
        scope["state"] = {**(scope.get("state") or {}), "mcp_principal": principal}
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await _serve(scope, replay, send)


async def _serve(scope, receive, send) -> None:
    """One stateless MCP exchange — what the SDK's session manager does per request,
    without needing its app-lifespan task group."""
    server = mcp._mcp_server
    transport = StreamableHTTPServerTransport(mcp_session_id=None, is_json_response_enabled=True)

    async def run_server(*, task_status=anyio.TASK_STATUS_IGNORED):
        async with transport.connect() as (read_stream, write_stream):
            task_status.started()
            await server.run(read_stream, write_stream,
                             server.create_initialization_options(), stateless=True)

    async with anyio.create_task_group() as tg:
        await tg.start(run_server)
        await transport.handle_request(scope, receive, send)
        tg.cancel_scope.cancel()


async def protected_resource_metadata(request):
    """RFC 9728 metadata for /mcp. Bearer header only; there is no OAuth server."""
    if not mcp_auth.is_enabled():
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    base = _base_url(request.scope)
    return JSONResponse({
        "resource": f"{base}{MCP_PATH}",
        "bearer_methods_supported": ["header"],
        "resource_name": "LlamaBot builder (Leo)",
    })


def register(app) -> None:
    """Serve ``/mcp`` and its metadata on ``app``."""
    app.router.routes.append(Route(MCP_PATH, endpoint=McpEndpoint(), include_in_schema=False))
    for path in (METADATA_PATH, f"{METADATA_PATH}{MCP_PATH}"):
        app.router.routes.append(Route(path, endpoint=protected_resource_metadata,
                                       methods=["GET"], include_in_schema=False))
