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

from app.models import User, ThreadMetadata
from app.dependencies import get_db_session, auth, admin_required
from app.services.thread_service import get_thread_list
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


class CreatePromptRequest(BaseModel):
    name: str
    content: str
    group: str = "General"
    description: str | None = None


class UpdatePromptRequest(BaseModel):
    name: str | None = None
    content: str | None = None
    group: str | None = None
    description: str | None = None
    is_active: bool | None = None


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
    before: Optional[str] = Query(None, description="Cursor for pagination - ISO timestamp"),
    limit: int = Query(10, ge=1, le=50)
):
    """Get recent conversation threads with cursor-based pagination (fast - metadata only).

    This endpoint queries the lightweight ThreadMetadata table instead of loading
    full LangGraph checkpoint states, providing a massive performance improvement.
    """
    from datetime import datetime
    from app.db import engine

    if engine is None:
        logger.error("Database engine not available")
        return {"threads": [], "next_cursor": None, "has_more": False}

    with Session(engine) as session:
        # Parse cursor timestamp
        before_dt = None
        if before:
            try:
                before_dt = datetime.fromisoformat(before.replace('Z', '+00:00'))
            except ValueError:
                logger.warning(f"Invalid cursor timestamp: {before}")

        # Query lightweight metadata (no checkpoint loading!)
        threads = get_thread_list(session, before=before_dt, limit=limit + 1)

        # Check if there are more results
        has_more = len(threads) > limit
        threads = threads[:limit]

        # Build response
        next_cursor = threads[-1].updated_at.isoformat() if threads else None

        logger.info(f"Returning {len(threads)} threads from metadata table (fast query)")

        return {
            "threads": [
                {
                    "thread_id": t.thread_id,
                    "title": t.title,
                    "created_at": t.created_at.isoformat(),
                    "updated_at": t.updated_at.isoformat(),
                    "message_count": t.message_count,
                    "agent_name": t.agent_name
                }
                for t in threads
            ],
            "next_cursor": next_cursor,
            "has_more": has_more
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


@router.get("/api/available-models", response_class=JSONResponse)
async def available_models():
    """Get list of available LLM models based on configured API keys.

    Returns which models are available (have API keys) and which are not.
    Frontend uses this to disable unavailable models in the dropdown.
    """
    # Map of model frontend values to their required API key env vars
    model_api_keys = {
        "claude-4.5-haiku": "ANTHROPIC_API_KEY",
        "claude-4.5-sonnet": "ANTHROPIC_API_KEY",
        "gpt-5-mini": "OPENAI_API_KEY",
        "gpt-5-codex": "OPENAI_API_KEY",
        "gemini-3-flash": "GOOGLE_API_KEY",
        "gemini-3-pro": "GOOGLE_API_KEY",
        "deepseek-chat": "DEEPSEEK_API_KEY",
        "deepseek-reasoner": "DEEPSEEK_API_KEY",
    }

    models = []
    for model_value, env_var in model_api_keys.items():
        api_key = os.environ.get(env_var, "")
        has_key = bool(api_key and api_key.strip())

        models.append({
            "value": model_value,
            "available": has_key,
            "reason": None if has_key else f"{env_var} not configured in .env"
        })

    return {"models": models}


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


@router.post("/api/update-activity", response_class=JSONResponse)
async def update_activity(request: Request, username: str = Depends(auth)):
    """Update last activity timestamp (called by frontend on user activity)."""
    from datetime import datetime, timezone
    request.app.state.timestamp = datetime.now(timezone.utc)
    return {"timestamp": request.app.state.timestamp.isoformat()}


@router.get("/api/lease-status", response_class=JSONResponse)
async def get_lease_status(request: Request):
    """Debug endpoint: show lease manager status."""
    from datetime import datetime, timezone

    mothership = getattr(request.app.state, 'mothership_client', None)
    last_activity = getattr(request.app.state, 'timestamp', None)
    now = datetime.now(timezone.utc)

    if mothership is None:
        return {
            "mothership_enabled": False,
            "error": "Mothership client not initialized"
        }

    return {
        "mothership_enabled": mothership.enabled,
        "instance_name": mothership.instance_name,
        "last_activity": last_activity.isoformat() if last_activity else None,
        "seconds_since_activity": (now - last_activity).total_seconds() if last_activity else None,
        "lease_duration_seconds": mothership.lease_duration_seconds,
    }


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


# ============== Prompt Library API ==============

@router.get("/api/prompts", response_class=JSONResponse)
async def api_get_prompts(
    username: str = Depends(auth),
    session: Session = Depends(get_db_session),
    group: Optional[str] = Query(None, description="Filter by group"),
    search: Optional[str] = Query(None, description="Search term")
):
    """Get all prompts, optionally filtered by group or search term."""
    from app.services.prompt_service import (
        get_all_prompts, get_prompts_by_group, search_prompts
    )

    if search:
        prompts = search_prompts(session, search)
    elif group:
        prompts = get_prompts_by_group(session, group)
    else:
        prompts = get_all_prompts(session)

    return [
        {
            "id": p.id,
            "name": p.name,
            "content": p.content,
            "description": p.description,
            "group": p.group,
            "usage_count": p.usage_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in prompts
    ]


@router.get("/api/prompts/groups", response_class=JSONResponse)
async def api_get_prompt_groups(
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Get list of unique prompt groups."""
    from app.services.prompt_service import get_prompt_groups
    groups = get_prompt_groups(session)
    return {"groups": groups}


@router.get("/api/prompts/{prompt_id}", response_class=JSONResponse)
async def api_get_prompt(
    prompt_id: int,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Get a specific prompt by ID."""
    from app.services.prompt_service import get_prompt_by_id
    prompt = get_prompt_by_id(session, prompt_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    return {
        "id": prompt.id,
        "name": prompt.name,
        "content": prompt.content,
        "description": prompt.description,
        "group": prompt.group,
        "usage_count": prompt.usage_count,
        "created_at": prompt.created_at.isoformat() if prompt.created_at else None,
        "updated_at": prompt.updated_at.isoformat() if prompt.updated_at else None,
    }


@router.post("/api/prompts", response_class=JSONResponse)
async def api_create_prompt(
    request: CreatePromptRequest,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Create a new prompt."""
    from app.services.prompt_service import create_prompt

    if not request.name or not request.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    if not request.content or not request.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")

    prompt = create_prompt(
        session,
        name=request.name,
        content=request.content,
        group=request.group,
        description=request.description
    )

    return {
        "id": prompt.id,
        "name": prompt.name,
        "message": "Prompt created successfully"
    }


@router.patch("/api/prompts/{prompt_id}", response_class=JSONResponse)
async def api_update_prompt(
    prompt_id: int,
    request: UpdatePromptRequest,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Update an existing prompt."""
    from app.services.prompt_service import update_prompt

    prompt = update_prompt(
        session, prompt_id,
        name=request.name,
        content=request.content,
        group=request.group,
        description=request.description,
        is_active=request.is_active
    )

    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    return {"message": "Prompt updated successfully"}


@router.delete("/api/prompts/{prompt_id}", response_class=JSONResponse)
async def api_delete_prompt(
    prompt_id: int,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session),
    hard_delete: bool = Query(False, description="Permanently delete")
):
    """Delete a prompt (soft delete by default)."""
    from app.services.prompt_service import delete_prompt

    if not delete_prompt(session, prompt_id, hard_delete=hard_delete):
        raise HTTPException(status_code=404, detail="Prompt not found")

    return {"message": "Prompt deleted successfully"}


@router.post("/api/prompts/{prompt_id}/use", response_class=JSONResponse)
async def api_use_prompt(
    prompt_id: int,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Increment usage count when a prompt is attached to a message."""
    from app.services.prompt_service import increment_usage

    prompt = increment_usage(session, prompt_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    return {"usage_count": prompt.usage_count}
