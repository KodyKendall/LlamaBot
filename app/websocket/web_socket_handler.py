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

logger = logging.getLogger(__name__)

# Authentication configuration
WS_AUTH_REQUIRED = os.getenv("WS_AUTH_REQUIRED", "false").lower() == "true"

# Frame fields that are credentials, not data. They must never reach the logs:
# `api_token` is a bearer credential for a specific Rails user (30-min TTL), and
# this handler logs whole frames. Before this existed, every chat turn from the
# Rails-embedded UI wrote a live user token into chat_app.log in plaintext.
_REDACTED_FRAME_KEYS = frozenset({"api_token", "token"})


def _redact_frame(frame):
    """Copy of `frame` with credential fields masked, for logging.

    Returns the frame unchanged if it isn't dict-like — callers log arbitrary
    payloads and a logging helper must never be the thing that raises.
    """
    try:
        items = frame.items()
    except AttributeError:
        return frame
    return {
        k: ("<redacted>" if k in _REDACTED_FRAME_KEYS and v else v)
        for k, v in items
    }


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

        Only browser JWTs carry a role claim, so only they are enforced:
          * ``rails_auth`` — the llama_bot_rails gem, a trusted internal caller
            with no role to check (see token_service.verify_rails_token).
          * unauthenticated — WS_AUTH_REQUIRED already governs that path below.
        """
        agent_name = json_data.get("agent_name")
        if not agent_name:
            return True
        if not self.auth_user or self.auth_user.get("type") != "ws_auth":
            return True

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

                    # For all other messages, check authentication
                    # First try to extract token from message (Rails gem pattern)
                    await self._check_auth_from_message(json_data)

                    if not self.authenticated:
                        if WS_AUTH_REQUIRED:
                            # Auth required but not authenticated - reject and close
                            logger.warning(f"{self._log_ctx()} Unauthenticated message rejected from {self.websocket.client}")
                            await self.manager.send_personal_message({
                                "type": "auth_error",
                                "content": "Authentication required. Please refresh the page."
                            }, self.websocket)
                            break
                        else:
                            # Auth not required but not authenticated - warn once (for migration)
                            if not auth_warning_sent:
                                logger.info(f"Unauthenticated WebSocket from {self.websocket.client} (auth not required)")
                                auth_warning_sent = True

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
                            "content": f"Error 80: {str(e)}"
                        }, self.websocket)
        except Exception as e:
            logger.error(f"{self._log_ctx()} WebSocket error: {str(e)}")
            # Only send error message if WebSocket is still open
            if self._is_websocket_open(self.websocket):
                await self.manager.send_personal_message({
                    "type": "error",
                    "content": f"Error 253: {str(e)}"
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
