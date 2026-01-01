"""API routes for LlamaBot."""

import json
import logging
import re
import os

from typing import Optional
from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session

from app.models import User
from app.dependencies import get_db_session, auth, admin_required
from app.services.user_service import (
    get_all_users, get_user_by_username, update_user, delete_user
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ============== Pydantic Models ==============

class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False
    role: str = "engineer"


class UpdateUserRequest(BaseModel):
    is_active: bool | None = None
    is_admin: bool | None = None
    new_password: str | None = None
    role: str | None = None


# ============== User Management API ==============

@router.get("/api/users", response_class=JSONResponse)
async def api_get_users(
    admin: User = Depends(admin_required),
    session: Session = Depends(get_db_session)
):
    """Get all users (admin only)."""
    users = get_all_users(session)
    return [
        {
            "id": u.id,
            "username": u.username,
            "is_active": u.is_active,
            "is_admin": u.is_admin,
            "role": getattr(u, 'role', 'engineer'),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "updated_at": u.updated_at.isoformat() if u.updated_at else None,
        }
        for u in users
    ]


@router.post("/api/users", response_class=JSONResponse)
async def api_create_user(
    request: CreateUserRequest,
    admin: User = Depends(admin_required),
    session: Session = Depends(get_db_session)
):
    """Create a new user (admin only)."""
    from app.services.user_service import hash_password, sanitize_username

    # Sanitize username (strip whitespace)
    try:
        clean_username = sanitize_username(request.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Check if username exists
    if get_user_by_username(session, clean_username):
        raise HTTPException(status_code=409, detail="Username already exists")

    user = User(
        username=clean_username,
        password_hash=hash_password(request.password),
        is_admin=request.is_admin,
        role=request.role
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    logger.info(f"Admin '{admin.username}' created user '{clean_username}' with role '{request.role}'")
    return {"id": user.id, "username": user.username, "message": "User created successfully"}


@router.patch("/api/users/{user_id}", response_class=JSONResponse)
async def api_update_user(
    user_id: int,
    request: UpdateUserRequest,
    admin: User = Depends(admin_required),
    session: Session = Depends(get_db_session)
):
    """Update a user (admin only)."""
    # Prevent admin from deactivating themselves
    if request.is_active is False and user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    # Prevent admin from removing their own admin status
    if request.is_admin is False and user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot remove your own admin status")

    user = update_user(
        session, user_id,
        is_active=request.is_active,
        is_admin=request.is_admin,
        new_password=request.new_password,
        role=request.role
    )

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    logger.info(f"Admin '{admin.username}' updated user {user_id}")
    return {"message": "User updated successfully"}


@router.delete("/api/users/{user_id}", response_class=JSONResponse)
async def api_delete_user(
    user_id: int,
    admin: User = Depends(admin_required),
    session: Session = Depends(get_db_session)
):
    """Delete a user (admin only)."""
    # Prevent admin from deleting themselves
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    if not delete_user(session, user_id):
        raise HTTPException(status_code=404, detail="User not found")

    logger.info(f"Admin '{admin.username}' deleted user {user_id}")
    return {"message": "User deleted successfully"}


# ============== Thread/Chat History API ==============

@router.get("/threads", response_class=JSONResponse)
async def threads(
    request: Request,
    username: str = Depends(auth),
    before: Optional[str] = Query(None, description="Cursor for pagination - timestamp of oldest thread from previous batch"),
    limit: int = Query(3, ge=1, le=20)
):
    """Get recent conversation threads with cursor-based pagination."""
    app = request.app
    checkpointer = app.state.get_or_create_checkpointer()

    seen_threads = set()
    recent_threads = []  # List of (thread_id, timestamp) tuples

    # Use async iteration with limit to avoid loading all checkpoints
    # Fetch extra since we're deduplicating by thread_id
    async for checkpoint in checkpointer.alist(config={}, limit=limit * 10):
        thread_id = checkpoint.config["configurable"]["thread_id"]
        # Get timestamp from checkpoint metadata
        timestamp = checkpoint.metadata.get("created_at") or checkpoint.config.get("configurable", {}).get("checkpoint_id", "")

        # Skip if we've seen this thread
        if thread_id in seen_threads:
            continue
        # Skip if timestamp is >= cursor (we want older threads)
        if before and timestamp >= before:
            continue

        seen_threads.add(thread_id)
        recent_threads.append((thread_id, timestamp))

        if len(recent_threads) >= limit:
            break

    logger.info(f"Returning {len(recent_threads)} threads (cursor-based pagination)")

    # Use cached graph from startup (singleton pattern)
    graph = app.state.compiled_graphs.get("llamabot")
    if not graph:
        from app.agents.llamabot.nodes import build_workflow
        graph = build_workflow(checkpointer=checkpointer)
        logger.warning("/threads endpoint using fallback graph compilation")

    # Fetch states (only for the limited threads)
    state_history = []
    for thread_id, timestamp in recent_threads:
        config = {"configurable": {"thread_id": thread_id}}
        state_history.append({
            "thread_id": thread_id,
            "timestamp": timestamp,
            "state": await graph.aget_state(config=config)
        })

    # Build response with next cursor
    next_cursor = recent_threads[-1][1] if recent_threads else None

    return {
        "threads": state_history,
        "next_cursor": next_cursor,
        "has_more": len(recent_threads) == limit
    }


@router.get("/chat-history/{thread_id}")
async def chat_history(thread_id: str, request: Request, username: str = Depends(auth)):
    """Get chat history for a specific thread."""
    app = request.app
    checkpointer = app.state.get_or_create_checkpointer()

    # Use cached graph from startup (singleton pattern)
    graph = app.state.compiled_graphs.get("llamabot")
    if not graph:
        from app.agents.llamabot.nodes import build_workflow
        graph = build_workflow(checkpointer=checkpointer)
        logger.warning("/chat-history endpoint using fallback graph compilation")

    config = {"configurable": {"thread_id": thread_id}}
    state_history = await graph.aget_state(config=config)
    print(state_history)
    return state_history


# ============== Other API Endpoints ==============

@router.get("/available-agents", response_class=JSONResponse)
async def available_agents():
    """Get list of available agents from langgraph.json."""
    with open("langgraph.json", "r") as f:
        langgraph_json = json.load(f)
    return {"agents": list(langgraph_json["graphs"].keys())}


@router.get("/rails-routes", response_class=JSONResponse)
async def rails_routes():
    """Parse routes.rb and return available GET routes for `index` actions and home page."""
    routes = []
    routes_file = "rails/config/routes.rb"

    # Check if routes file exists
    if not os.path.exists(routes_file):
        return {"routes": [{"path": "/", "name": "Home"}]}

    try:
        with open(routes_file, "r") as f:
            content = f.read()

        # Extract root path
        root_match = re.search(r'root\s+"([^"]+)#([^"]+)"', content)
        if root_match:
            routes.append({"path": "/", "name": "Home"})

        # Extract explicit home route
        home_match = re.search(r'get\s+"home"\s*=>', content)
        if home_match and not any(r["path"] == "/" for r in routes):
            routes.append({"path": "/", "name": "Home"})

        # Extract resources (which create index routes)
        resource_matches = re.findall(r'resources\s+:(\w+)', content)
        for resource in resource_matches:
            routes.append({
                "path": f"/{resource}",
                "name": resource.capitalize()
            })

        # Extract custom GET routes
        get_matches = re.findall(r'get\s+"([^"]+)"\s*=>\s*"([^"]+)#([^"]+)"', content)
        for path, controller, action in get_matches:
            if path not in ["/", "home", "up", "service-worker", "manifest"]:
                display_name = path.replace("/", "").replace("_", " ").title() or "Home"
                routes.append({
                    "path": f"/{path}" if not path.startswith("/") else path,
                    "name": display_name
                })

        # Remove duplicates based on path
        seen = set()
        unique_routes = []
        for route in routes:
            if route["path"] not in seen:
                seen.add(route["path"])
                unique_routes.append(route)

        return {"routes": unique_routes}

    except Exception as e:
        print(f"Error parsing routes: {e}")
        return {"routes": [{"path": "/", "name": "Home"}]}


@router.get("/check")
def check_timestamp(request: Request):
    """Returns the timestamp of last message from user in UTC."""
    return {"timestamp": request.app.state.timestamp}


@router.get("/api/instance-info", response_class=JSONResponse)
async def get_instance_info():
    """Get instance info from .leonardo/instance.json if it exists."""
    instance_file = ".leonardo/instance.json"

    if not os.path.exists(instance_file):
        return {"instance_name": None}

    try:
        with open(instance_file, "r") as f:
            data = json.load(f)
        return {"instance_name": data.get("instance_name")}
    except Exception as e:
        logger.warning(f"Error reading instance.json: {e}")
        return {"instance_name": None}


@router.post("/api/capture-rails-logs", response_class=JSONResponse)
async def capture_rails_logs_endpoint(request: Request, username: str = Depends(auth)):
    """Capture Rails logs for 10 seconds and return the content."""
    from pathlib import Path
    from app.agents.leonardo.rails_agent.tools import capture_rails_logs

    file_path = capture_rails_logs(duration=10)
    logs = Path(file_path).read_text()

    return {"logs": logs, "file_path": file_path}
