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
            logger.warning(f"WebSocket auth failed from {self.websocket.client}")
            await self.manager.send_personal_message({
                "type": "auth_error",
                "content": "Invalid or expired token"
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

    async def handle_websocket(self):
        logger.info(f"New WebSocket connection attempt from {self.websocket.client}")
        await self.manager.connect(self.websocket)
        current_task = None

        # Track if we've sent an auth warning (only send once)
        auth_warning_sent = False

        try:
            while True:
                try:
                    start_time = asyncio.get_event_loop().time()

                    logger.info("Waiting for message from LlamaPress")
                    json_data = await self.websocket.receive_json()

                    receive_time = asyncio.get_event_loop().time()

                    ### Warning: If LangGraph does await LLM calls appropriately, then this main thread can get blocked and will stop responding to pings from LlamaPress, ultimately killing the websocket connection.
                    logger.info(f"Message received after {receive_time - start_time:.2f}s")
                    logger.info(f"Received message from LlamaPress!")

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
                            logger.warning(f"WebSocket auth failed, closing connection from {self.websocket.client}")
                            break
                        continue

                    # Handle cancel (always allowed)
                    if isinstance(json_data, dict) and json_data.get("type") == "cancel":
                        logger.info("CANCEL RECV")
                        if current_task and not current_task.done():
                            current_task.cancel()
                            # Only send if WebSocket is still open
                            if self._is_websocket_open(self.websocket):
                                await self.manager.send_personal_message({
                                    "type": "system_message",
                                    "content": "Previous task has been cancelled"
                                }, self.websocket)
                        continue

                    # For all other messages, check authentication
                    # First try to extract token from message (Rails gem pattern)
                    await self._check_auth_from_message(json_data)

                    if not self.authenticated:
                        if WS_AUTH_REQUIRED:
                            # Auth required but not authenticated - reject and close
                            logger.warning(f"Unauthenticated message rejected from {self.websocket.client}")
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

                    # Cancel previous task if it exists and create new one
                    if current_task and not current_task.done():
                        logger.info("Cancelling previous task")
                        current_task.cancel()
                        try:
                            await current_task
                        except asyncio.CancelledError:
                            logger.info("Previous task was cancelled successfully")

                    message = ChatMessage(**json_data)

                    logger.info(f"Received message: {message}")
                    current_task = asyncio.create_task(
                        self.request_handler.handle_request(message, self.websocket)
                    )
                except WebSocketDisconnect as e:
                    if e.code == 1000:
                        logger.info(f"WebSocket connection closed gracefully by client: {e.reason}")
                    else:
                        logger.warning(f"WebSocket disconnected with unexpected code: {e.code} {e.reason}")
                    break
                except Exception as e:
                    logger.error(f"WebSocket error: {str(e)}")
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
            logger.error(f"WebSocket error: {str(e)}")
            # Only send error message if WebSocket is still open
            if self._is_websocket_open(self.websocket):
                await self.manager.send_personal_message({
                    "type": "error",
                    "content": f"Error 253: {str(e)}"
                }, self.websocket)
        finally:
            if current_task and not current_task.done():
                current_task.cancel()
                try:
                    logger.info("Cancelling current task")
                    await current_task
                except asyncio.CancelledError:
                    logger.info("Current task was cancelled successfully")
                    pass
            self.manager.disconnect(self.websocket)
            self.request_handler.cleanup_connection(self.websocket)
