"""WebSocket route for LlamaBot."""

from fastapi import APIRouter, WebSocket

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time chat communication."""
    # Import here to avoid circular imports - manager is initialized in main.py
    from app.main import manager
    from app.websocket.web_socket_handler import WebSocketHandler

    await WebSocketHandler(websocket, manager).handle_websocket()
