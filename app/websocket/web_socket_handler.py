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

    def _is_websocket_open(self, websocket: WebSocket) -> bool:
        """Check if the WebSocket connection is still open"""
        return websocket.client_state == WebSocketState.CONNECTED

    async def handle_websocket(self):
        logger.info(f"New WebSocket connection attempt from {self.websocket.client}")
        await self.manager.connect(self.websocket)
        current_task = None
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

                    if isinstance(json_data, dict) and json_data.get("type") == "ping":
                        logger.info("PING RECV, SENDING PONG")
                        #prevent batch queue
                        await asyncio.shield(
                            self.manager.send_personal_message({"type": "pong"}, self.websocket)
                        )
                        continue
                    
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