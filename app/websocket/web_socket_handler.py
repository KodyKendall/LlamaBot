from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
import asyncio

import os
import logging
import time
import json

from pydantic import BaseModel

from app.websocket.web_socket_connection_manager import WebSocketConnectionManager
from app.websocket.request_handler import RequestHandler
from app.websocket.error_text import describe_exception

logger = logging.getLogger(__name__)

# Authentication configuration
# Defaults ON. It shipped defaulting to false, which meant a socket that simply
# never sent an `auth` frame was marked authenticated and could run any agent —
# and no provisioner set the variable, so every box ran with it off. Leonardo's
# .env.example has said `WS_AUTH_REQUIRED=true` all along; this makes the code
# agree with the documented intent. Self-hosters who genuinely want an open box
# can still set it to false explicitly.
WS_AUTH_REQUIRED = os.getenv("WS_AUTH_REQUIRED", "true").lower() == "true"

# Frames that act on an already-running turn rather than starting one. They are
# refused without authentication like anything else, but refusing one drops the
# frame instead of closing the socket — see handle_websocket.
_CONTROL_FRAME_TYPES = frozenset({
    "cancel", "attach", "approval_response", "question_response",
})

# Frame fields that are credentials, not data. They must never reach the logs:
# `api_token` is a bearer credential for a specific Rails user (30-min TTL), and
# this handler logs whole frames. Before this existed, every chat turn from the
# Rails-embedded UI wrote a live user token into chat_app.log in plaintext.
_REDACTED_FRAME_KEYS = frozenset({"api_token", "token"})

# Per-field cap for the LOG copy of a frame. Redaction alone doesn't truncate, so
# a page-context payload used to write its full 10.6 MB into the docker logs on
# every single message (SupportIncident #246). Kept generous enough that
# `docker compose logs llamabot | grep -a "Received message:" | wc -L` is still
# the triage command — it just can't be tens of megabytes any more.
_LOGGED_VALUE_MAX_BYTES = 2048


def _redact_frame(frame):
    """Copy of `frame` with credential fields masked and big values truncated.

    Returns the frame unchanged if it isn't dict-like — callers log arbitrary
    payloads and a logging helper must never be the thing that raises.
    """
    from app.lib.text_budget import truncate_text

    try:
        items = frame.items()
    except AttributeError:
        return frame

    def _for_log(key, value):
        if key in _REDACTED_FRAME_KEYS and value:
            return "<redacted>"
        try:
            rendered = value if isinstance(value, str) else repr(value)
            if len(rendered.encode("utf-8", "ignore")) <= _LOGGED_VALUE_MAX_BYTES:
                return value
            return truncate_text(rendered, _LOGGED_VALUE_MAX_BYTES)
        except Exception:
            return value

    return {k: _for_log(k, v) for k, v in items}


# Pydantic model for chat request
class ChatMessage(dict):
    message: str
    thread_id: str = None  # Optional thread_id parameter
    agent: str = None  # Optional agent parameter

class WebSocketHandler:
    def __init__(self, websocket: WebSocket, manager: WebSocketConnectionManager):
        self.websocket = websocket
        self.manager = manager
        self.request_handler = RequestHandler(manager.app)

        # Authentication state
        self.authenticated = not WS_AUTH_REQUIRED  # Auto-auth if auth not required
        self.auth_user = None

        # Threads this connection has driven/subscribed to. Background runs are
        # owned by the app-level RunManager, not this connection — on disconnect
        # we DETACH from these (so the run keeps going), we do not cancel them.
        self._attached_threads: set = set()

        # Log correlation. Without these, an open/close pair in chat_app.log cannot
        # be tied to the thread it was serving, which is exactly what made Lohman's
        # "the connection is lost" feedback untriageable (2026-07-06).
        self._init_connection_id()
        self.current_thread_id = None
        self.current_agent_name = None
        self.current_llm_model = None

    def _init_connection_id(self) -> None:
        """Short, unique-per-socket id that every log line for this socket carries."""
        import secrets
        self.connection_id = secrets.token_hex(4)

    def _log_ctx(self) -> str:
        """`[ws=<conn> thread=<tid>]` prefix for this connection's log lines."""
        tid = getattr(self, "current_thread_id", None)
        return f"[ws={self.connection_id}" + (f" thread={tid}]" if tid else "]")

    def _note_message_context(self, data) -> None:
        """Remember thread/agent/model from an inbound frame for later log lines.

        Only ever overwrites with a present value — a ping or control frame must not
        wipe the correlation we already established.
        """
        if not isinstance(data, dict):
            return
        for attr, key in (
            ("current_thread_id", "thread_id"),
            ("current_agent_name", "agent_name"),
            ("current_llm_model", "llm_model"),
        ):
            value = data.get(key)
            if value:
                setattr(self, attr, value)

    def _is_websocket_open(self, websocket: WebSocket) -> bool:
        """Check if the WebSocket connection is still open"""
        return websocket.client_state == WebSocketState.CONNECTED

    async def _handle_auth_message(self, data: dict) -> bool:
        """
        Handle authentication message.

        Returns True if authenticated successfully, False otherwise.
        """
        from app.services.token_service import verify_ws_token, verify_rails_token, is_rails_token

        token = data.get("token")
        if not token:
            await self.manager.send_personal_message({
                "type": "auth_error",
                "content": "Token required"
            }, self.websocket)
            return False

        # Try JWT token first (from browser clients)
        payload = verify_ws_token(token)

        # If not a valid JWT, try Rails token (from llama_bot_rails gem)
        if not payload and is_rails_token(token):
            payload = verify_rails_token(token)

        if payload:
            self.authenticated = True
            self.auth_user = payload
            # Stamp the turn owner for the life of this connection's task, so
            # get_llm knows whose ChatGPT subscription (if any) to spend. Rails
            # tokens carry no user_id — that leaves it None and the
            # subscription models fail open to the operator default, which is
            # the intended behavior. See app/lib/request_context.py.
            from app.lib.request_context import set_current_user_id
            set_current_user_id(payload.get("user_id"))
            # Stamp WHO this connection's turns belong to, so every mothership
            # report fired from inside the run (report_message, report_error,
            # report_turn_metrics) is attributable to a person rather than just
            # to this box. One DB read per connection, not per message; the
            # RequestHandler is built before auth, so there is nowhere to thread
            # the user through by hand. See app/services/user_context.py.
            from app.services import user_context
            user_context.set_current(
                user_context.for_user_id(payload.get("user_id"))
                or user_context.from_token_payload(payload)
            )
            await self.manager.send_personal_message({
                "type": "auth_success",
                "user": payload.get("sub")
            }, self.websocket)
            return True
        else:
            logger.warning(f"{self._log_ctx()} WebSocket auth failed from {self.websocket.client}")
            await self.manager.send_personal_message({
                "type": "auth_error",
                "content": "Invalid or expired token"
            }, self.websocket)
            return False

    async def _authorize_agent_mode(self, json_data: dict) -> bool:
        """Gate any frame naming an ``agent_name`` against the sender's role grant.

        This is THE server-side check for agent modes. The dropdown filtering in
        chat.html is a convenience — this is the control. Both read the same
        resolver (app/permissions.py) so they can't drift.

        Frames with no ``agent_name`` (ping, cancel, attach) pass straight through,
        which is why this sits before the type dispatch: any future frame type
        that names a mode is covered without touching this function.

        Every authenticated caller carries a role, so all of them are enforced
        the same way — browser JWTs by the user's own role, the llama_bot_rails
        gem by the ``rails`` role. This used to `return True` for anything that
        was not a browser JWT, on the premise that ``rails_auth`` meant a
        trusted internal caller. Nothing verified that premise (any string with
        a ``--`` in it produced a ``rails_auth`` payload), so the branch was an
        open door: an anonymous socket naming ``rails_agent`` ran the engineer
        agent, shell tools and all. It fails closed now, and
        ``verify_rails_token`` actually checks the signature.

        The one caller still allowed through unauthenticated is a box whose
        operator has explicitly set ``WS_AUTH_REQUIRED=false``. Denying there
        would make that flag mean "chat is broken" rather than "this box has no
        auth"; the default is now on, so it is a deliberate choice.
        """
        agent_name = json_data.get("agent_name")
        if not agent_name:
            return True

        if not self.auth_user:
            if not WS_AUTH_REQUIRED:
                return True
            logger.warning(
                f"Denied agent '{agent_name}' to an unauthenticated client "
                f"from {self.websocket.client}"
            )
            if self._is_websocket_open(self.websocket):
                await self.manager.send_personal_message({
                    "type": "auth_error",
                    "content": "Authentication required. Please refresh the page.",
                }, self.websocket)
            return False

        from sqlmodel import Session

        from app.db import engine
        from app.permissions import can_run_agent, custom_modes

        role = self.auth_user.get("role")
        is_admin = bool(self.auth_user.get("is_admin"))
        try:
            with Session(engine) as session:
                permitted = can_run_agent(session, role, is_admin, agent_name, custom_modes())
        except Exception as e:
            # Fail CLOSED: if we can't establish the grant, don't run the agent.
            logger.error(f"Agent authorization failed for '{agent_name}', denying: {e}")
            permitted = False

        if permitted:
            return True

        logger.warning(
            f"Denied agent '{agent_name}' to user '{self.auth_user.get('sub')}' "
            f"(role={role}, is_admin={is_admin}) from {self.websocket.client}"
        )
        if self._is_websocket_open(self.websocket):
            await self.manager.send_personal_message({
                "type": "error",
                "content": "You don't have access to that mode.",
            }, self.websocket)
        return False

    async def _check_auth_from_message(self, json_data: dict) -> bool:
        """
        Check for authentication token in regular message (Rails gem pattern).

        The Rails gem passes api_token in the message payload rather than
        a separate auth message.

        Returns True if authenticated (or already was), False if auth required but missing.
        """
        if self.authenticated:
            return True

        # Check for api_token in message (Rails gem pattern)
        api_token = json_data.get("api_token")
        if api_token:
            return await self._handle_auth_message({"token": api_token})

        return False

    async def _handle_attach(self, json_data: dict) -> None:
        """Resume a background run after (re)connect: replay missed output, then live-tail.

        The client sends ``{type:"attach", thread_id, last_seq}`` with the
        highest seq it has already rendered. We mark this socket as the run's
        live subscriber and replay everything after ``last_seq``. If the missed
        window was evicted (gap) or there's no run for the thread (finished long
        ago / process restarted), we tell the client to reload thread history.
        The client dedupes by seq, so any overlap between this replay and the
        live tail is harmless.
        """
        thread_id = str(json_data.get("thread_id"))
        try:
            last_seq = int(json_data.get("last_seq") or 0)
        except (TypeError, ValueError):
            last_seq = 0

        self._attached_threads.add(thread_id)
        run_manager = self.request_handler._run_manager()
        handle = run_manager.attach(thread_id, self.websocket)

        if handle is None:
            await self.manager.send_personal_message({
                "type": "no_active_run",
                "thread_id": thread_id,
            }, self.websocket)
            return

        log = handle.log
        if log.has_gap(last_seq):
            # Missed messages were evicted — client must reload from checkpoint.
            await self.manager.send_personal_message({
                "type": "replay_gap",
                "thread_id": thread_id,
                "min_seq": log.min_seq,
            }, self.websocket)
        else:
            for entry in log.since(last_seq):
                await self.manager.send_personal_message(entry, self.websocket)

        await self.manager.send_personal_message({
            "type": "attached",
            "thread_id": thread_id,
            "status": log.status,
            "last_seq": log.last_seq,
        }, self.websocket)

    async def handle_websocket(self):
        logger.info(f"{self._log_ctx()} New WebSocket connection attempt from {self.websocket.client}")
        await self.manager.connect(self.websocket)

        # Track if we've sent an auth warning (only send once)
        auth_warning_sent = False

        try:
            while True:
                try:
                    start_time = asyncio.get_event_loop().time()

                    logger.info(f"{self._log_ctx()} Waiting for message from LlamaPress")
                    json_data = await self.websocket.receive_json()

                    receive_time = asyncio.get_event_loop().time()

                    # Capture thread/agent/model BEFORE any logging below, so every
                    # subsequent line for this socket (incl. the disconnect) is
                    # correlatable to the thread it was serving.
                    self._note_message_context(json_data)

                    ### Warning: If LangGraph does await LLM calls appropriately, then this main thread can get blocked and will stop responding to pings from LlamaPress, ultimately killing the websocket connection.
                    logger.info(f"{self._log_ctx()} Message received after {receive_time - start_time:.2f}s")
                    logger.info(
                        f"{self._log_ctx()} Received message from LlamaPress "
                        f"(agent={self.current_agent_name} model={self.current_llm_model})"
                    )

                    # Handle ping (always allowed, even unauthenticated)
                    if isinstance(json_data, dict) and json_data.get("type") == "ping":
                        logger.info("PING RECV, SENDING PONG")
                        #prevent batch queue
                        await asyncio.shield(
                            self.manager.send_personal_message({"type": "pong"}, self.websocket)
                        )
                        continue

                    # Handle explicit auth message
                    if isinstance(json_data, dict) and json_data.get("type") == "auth":
                        success = await self._handle_auth_message(json_data)
                        if not success and WS_AUTH_REQUIRED:
                            # Auth failed and required - close connection
                            logger.warning(f"{self._log_ctx()} WebSocket auth failed, closing connection from {self.websocket.client}")
                            break
                        continue

                    # Authenticate BEFORE any dispatch. This used to sit below
                    # the control-frame handlers, so `cancel`, `attach`,
                    # `approval_response` and `question_response` bypassed
                    # WS_AUTH_REQUIRED entirely: `attach` replays a background
                    # run's buffered output by thread_id, and
                    # `approval_response` resumes a run parked on a human
                    # approval — both reachable with no credentials at all.
                    # `ping` and `auth` above are the only pre-auth frames.
                    await self._check_auth_from_message(json_data)

                    if not self.authenticated:
                        if WS_AUTH_REQUIRED:
                            frame_type = json_data.get("type") if isinstance(json_data, dict) else None
                            logger.warning(
                                f"{self._log_ctx()} Unauthenticated '{frame_type or 'message'}' "
                                f"frame rejected from {self.websocket.client}"
                            )
                            if self._is_websocket_open(self.websocket):
                                await self.manager.send_personal_message({
                                    "type": "auth_error",
                                    "content": "Authentication required. Please refresh the page."
                                }, self.websocket)
                            if frame_type in _CONTROL_FRAME_TYPES:
                                # Drop the frame, keep the socket. A control
                                # frame can arrive just ahead of the `auth`
                                # frame on reconnect (the browser fetches its
                                # token asynchronously); closing here would
                                # turn that race into a reconnect loop.
                                continue
                            break
                        elif not auth_warning_sent:
                            # Auth not required but not authenticated - warn once (for migration)
                            logger.info(f"Unauthenticated WebSocket from {self.websocket.client} (auth not required)")
                            auth_warning_sent = True

                    # Authorize the agent mode BEFORE any dispatch. Sits here so
                    # every frame that names an agent_name is gated by one check
                    # — chat, approval_response, question_response, and anything
                    # added later. Frames without an agent_name pass through.
                    if isinstance(json_data, dict) and not await self._authorize_agent_mode(json_data):
                        continue

                    # Handle cancel (always allowed) — stop the background run(s)
                    # for the target thread (explicit user "stop").
                    if isinstance(json_data, dict) and json_data.get("type") == "cancel":
                        logger.info("CANCEL RECV")
                        run_manager = self.request_handler._run_manager()
                        target = json_data.get("thread_id")
                        targets = [str(target)] if target else list(self._attached_threads)
                        cancelled_any = False
                        for tid in targets:
                            if await run_manager.cancel(tid):
                                cancelled_any = True
                        if cancelled_any and self._is_websocket_open(self.websocket):
                            await self.manager.send_personal_message({
                                "type": "system_message",
                                "content": "Previous task has been cancelled"
                            }, self.websocket)
                        continue

                    # Handle attach — a (re)connecting client resuming a background
                    # run. Replays missed output then live-tails. See run_manager.py.
                    if isinstance(json_data, dict) and json_data.get("type") == "attach":
                        logger.info("ATTACH RECV")
                        await self._handle_attach(json_data)
                        continue

                    # Handle approval response (user approved/rejected a HITL tool call)
                    if isinstance(json_data, dict) and json_data.get("type") == "approval_response":
                        logger.info("APPROVAL_RESPONSE RECV")
                        tid = json_data.get("thread_id")
                        if tid:
                            self._attached_threads.add(str(tid))
                        await self.request_handler.start_resume_run(json_data, self.websocket, "approval")
                        continue

                    # Handle question response (user answered a plan mode question)
                    if isinstance(json_data, dict) and json_data.get("type") == "question_response":
                        logger.info("QUESTION_RESPONSE RECV")
                        tid = json_data.get("thread_id")
                        if tid:
                            self._attached_threads.add(str(tid))
                        await self.request_handler.start_resume_run(json_data, self.websocket, "question")
                        continue

                    # Idempotency guard (Layer 1 seatbelt): a dropped socket can
                    # make the browser re-send a message it already delivered
                    # (see resend-on-reconnect in frontend/chat/index.js). Without
                    # this guard the re-send would cancel the in-flight run and
                    # restart the agent from scratch. Recognize the duplicate by
                    # (thread_id, client_message_id), ACK it, and ignore it.
                    # Clients that don't send client_message_id (Rails gem, older
                    # browsers) are always treated as new.
                    client_message_id = json_data.get("client_message_id")
                    thread_id = json_data.get("thread_id")
                    is_new_message = self.manager.deduplicator.register(thread_id, client_message_id)
                    if client_message_id and self._is_websocket_open(self.websocket):
                        await self.manager.send_personal_message({
                            "type": "ack",
                            "client_message_id": client_message_id,
                            "status": "accepted" if is_new_message else "duplicate",
                        }, self.websocket)
                    if not is_new_message:
                        logger.info(
                            f"Ignoring duplicate message {client_message_id} on thread "
                            f"{thread_id} (idempotency guard); leaving in-flight run intact."
                        )
                        continue

                    message = ChatMessage(**json_data)
                    if thread_id:
                        self._attached_threads.add(str(thread_id))

                    logger.info(f"Received message: {_redact_frame(message)}")
                    # Start the turn as a background run owned by the per-thread
                    # RunManager (decoupled from this socket, which is attached as
                    # a live subscriber). A new message supersedes any in-flight
                    # run for the thread; that cancellation + thread-state repair
                    # is handled inside RunManager / _repair_thread_state_if_needed.
                    await self.request_handler.start_chat_run(message, self.websocket)
                except WebSocketDisconnect as e:
                    if e.code == 1000:
                        logger.info(f"{self._log_ctx()} WebSocket closed gracefully by client: code={e.code} reason={e.reason}")
                    else:
                        logger.warning(f"{self._log_ctx()} WebSocket disconnected unexpectedly: code={e.code} reason={e.reason}")
                    break
                except Exception as e:
                    logger.error(f"{self._log_ctx()} WebSocket error: {str(e)}")
                    # Break on disconnect-related errors to avoid infinite loop
                    if "not connected" in str(e).lower() or not self._is_websocket_open(self.websocket):
                        break
                    # Only send error message if WebSocket is still open
                    if self._is_websocket_open(self.websocket):
                        await self.manager.send_personal_message({
                            "type": "error",
                            # describe_exception keeps the class name — a
                            # message-less transport error (httpx.ReadError)
                            # otherwise renders as "Error 80: " and nothing else.
                            "content": f"Error 80: {describe_exception(e)}"
                        }, self.websocket)
        except Exception as e:
            logger.error(f"{self._log_ctx()} WebSocket error: {str(e)}")
            # Only send error message if WebSocket is still open
            if self._is_websocket_open(self.websocket):
                await self.manager.send_personal_message({
                    "type": "error",
                    "content": f"Error 253: {describe_exception(e)}"
                }, self.websocket)
        finally:
            # Detach from any background runs this connection subscribed to — but
            # DO NOT cancel them. A dropped browser must not kill an in-progress
            # build; the run keeps going and the client replays it on reconnect
            # via `attach`. Explicit cancellation only happens on a `cancel`
            # message or when a new message supersedes the run.
            run_manager = self.request_handler._run_manager()
            for tid in self._attached_threads:
                run_manager.detach(tid, self.websocket)
            self.manager.disconnect(self.websocket)
            self.request_handler.cleanup_connection(self.websocket)
