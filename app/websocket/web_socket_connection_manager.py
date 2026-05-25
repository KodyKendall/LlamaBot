from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
import asyncio
import logging
from typing import Union
logger = logging.getLogger(__name__)

class WebSocketConnectionManager:
    def __init__(self, app: FastAPI):
        self.app = app
        self.active_connections: list[WebSocket] = []
        self.active_tasks: set = set()
        self._connection_ids: set = set()  # Track unique connections

    def _is_websocket_open(self, websocket: WebSocket) -> bool:
        """Check if the WebSocket connection is still open"""
        return websocket.client_state == WebSocketState.CONNECTED

    async def connect(self, websocket: WebSocket):
        try:
            await websocket.accept()
            connection_id = id(websocket)
            if connection_id not in self._connection_ids:
                self._connection_ids.add(connection_id)
                self.active_connections.append(websocket)
                logger.info(f"New connection added. Total connections: {len(self.active_connections)}")
        except Exception as e: 
            logging.info(f"Connect Exception in WebSocketConnectionManager: {e}")

    def disconnect(self, websocket: WebSocket):
        try:
            connection_id = id(websocket)
            if connection_id in self._connection_ids:
                self._connection_ids.remove(connection_id)
                self.active_connections.remove(websocket)
                logger.info(f"Connection removed. Total connections: {len(self.active_connections)}")
        except Exception as e: 
            logging.info(f"Disconnect Exception in WebSocketConnectionManager: {e}")

    async def send_personal_message(self, message: Union[str, dict], websocket: WebSocket):
        # Only send if WebSocket is still open
        if self._is_websocket_open(websocket):
            try:
                if isinstance(message, dict):
                    await websocket.send_json(message)
                else:
                    await websocket.send_text(message)
            except Exception as e:
                logger.warning(f"Failed to send message to WebSocket: {e}")

    async def broadcast(self, message: str):
        # Create a copy of active connections to avoid modification during iteration
        connections_to_send = self.active_connections.copy()
        for connection in connections_to_send:
            if self._is_websocket_open(connection):
                try:
                    await connection.send_json({"message": message})
                except Exception as e:
                    logger.warning(f"Failed to broadcast message to WebSocket: {e}")
                    # Remove the connection if it's no longer valid
                    self.disconnect(connection)

    def cleanup(self):
        # Cancel all active tasks
        for task in self.active_tasks:
            if not task.done():
                task.cancel()
        self.active_tasks.clear()
