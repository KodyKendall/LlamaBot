"""API routes for LlamaBot."""

import json
import logging
import re
import os

from typing import Optional
from fastapi import APIRouter, Request, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session

from app.models import User, ThreadMetadata
from app.dependencies import get_db_session, auth, admin_required, engineer_or_admin_required, get_current_user
from app.services.thread_service import get_thread_list
from app.services.user_service import (
    get_all_users, get_user_by_username, update_user, delete_user
)
from app.agents.leonardo.model_capabilities import get_model_capabilities
from app.agents.leonardo.model_policy import is_model_enabled

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


class CreateSkillRequest(BaseModel):
    name: str
    content: str
    group: str = "General"
    description: str | None = None


class UpdateSkillRequest(BaseModel):
    name: str | None = None
    content: str | None = None
    group: str | None = None
    description: str | None = None
    is_active: bool | None = None


# ============== Version API ==============

def get_container_version() -> str:
    """Get the version from docker-compose.yml (mounted at /app/leonardo) or Docker API."""
    import socket

    # First, try to parse from mounted docker-compose.yml
    compose_paths = [
        "/app/leonardo/docker-compose.yml",
        "/app/leonardo/docker-compose-dev.yml",
    ]
    for compose_path in compose_paths:
        try:
            with open(compose_path, 'r') as f:
                for line in f:
                    # Look for image line with llamabot (commented or not)
                    # e.g., "# image: kody06/llamabot:0.3.5c" or "image: kody06/llamabot:0.3.5c"
                    if 'image:' in line and 'llamabot:' in line:
                        # Extract version from "kody06/llamabot:0.3.5c"
                        match = re.search(r'llamabot:([^\s"\']+)', line)
                        if match:
                            return match.group(1)
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug(f"Could not parse {compose_path}: {e}")

    # Fallback: Query Docker API
    try:
        container_id = socket.gethostname()
        import http.client
        conn = http.client.HTTPConnection("localhost")
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.connect("/var/run/docker.sock")
        conn.request("GET", f"/containers/{container_id}/json")
        response = conn.getresponse()

        if response.status == 200:
            data = json.loads(response.read().decode())
            image = data.get("Config", {}).get("Image", "")
            if ":" in image:
                version = image.split(":")[-1]
                if version != "latest":
                    return version
        return "dev"
    except Exception as e:
        logger.debug(f"Could not get container version: {e}")
        return "dev"


def get_llamapress_version() -> str:
    """Get the LlamaPress version from docker-compose.yml."""
    compose_paths = [
        "/app/leonardo/docker-compose.yml",
        "/app/leonardo/docker-compose-dev.yml",
    ]
    for compose_path in compose_paths:
        try:
            with open(compose_path, 'r') as f:
                for line in f:
                    if 'image:' in line and 'llamapress-simple:' in line:
                        match = re.search(r'llamapress-simple:([^\s"\']+)', line)
                        if match:
                            return match.group(1)
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug(f"Could not parse {compose_path} for llamapress version: {e}")
    return "dev"


@router.get("/api/version", response_class=JSONResponse)
async def api_get_version():
    """Get the current LlamaBot version from docker-compose.yml or Docker image tag."""
    version = get_container_version()
    return {"version": version}


def get_version_notes(version: str) -> list[str]:
    """
    Get release notes for a specific version from docs/dev_logs.

    Args:
        version: Full version string like "0.3.6h"

    Returns:
        List of note strings for that version, or empty list if not found
    """
    if not version or version == "dev":
        return []

    # Parse version to get base (e.g., "0.3.6" from "0.3.6h")
    # Version format: X.Y.Z or X.Y.Za where 'a' is optional letter suffix
    match = re.match(r'^(\d+\.\d+\.\d+)', version)
    if not match:
        return []

    base_version = match.group(1)

    # Try to read the dev_log file
    dev_log_paths = [
        f"/app/docs/dev_logs/{base_version}",
        f"/app/app/docs/dev_logs/{base_version}",  # Alternative path in container
    ]

    content = None
    for path in dev_log_paths:
        try:
            with open(path, 'r') as f:
                content = f.read()
            break
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug(f"Could not read dev_log {path}: {e}")
            continue

    if not content:
        return []

    # Find the section for this version (e.g., "0.3.6h:" or "0.3.6ec:")
    # Pattern: version at start of line followed by colon
    lines = content.split('\n')
    notes = []
    in_section = False

    for line in lines:
        stripped = line.strip()

        # Check if this is a version header (e.g., "0.3.6h:" or "0.3.6ec:")
        if re.match(r'^\d+\.\d+\.\d+[a-z]*:', stripped):
            if in_section:
                # We've reached the next version, stop collecting
                break
            # Check if this is our version
            if stripped.startswith(f"{version}:"):
                in_section = True
            continue

        # Collect bullet points while in our section
        if in_section and stripped.startswith('- '):
            # Clean up the note: remove "- [x] " or "- [ ] " prefix
            note = re.sub(r'^- \[[x ]\] ', '', stripped)
            note = re.sub(r'^- ', '', note)  # Also handle plain "- " prefix
            if note:
                notes.append(note)

    return notes


@router.get("/api/version-notes", response_class=JSONResponse)
async def api_get_version_notes():
    """Get the current version and its release notes from docs/dev_logs."""
    version = get_container_version()
    notes = get_version_notes(version)
    return {"version": version, "notes": notes}


@router.get("/api/check-updates", response_class=JSONResponse)
async def api_check_updates():
    """Check mothership for available updates to llamabot and llamapress."""
    from app.services.mothership_client import MothershipClient
    mothership = MothershipClient()
    if not mothership.enabled:
        return {"updates_available": False, "reason": "mothership_not_configured"}

    llamabot_version = get_container_version()
    llamapress_version = get_llamapress_version()

    result = await mothership.check_updates(llamabot_version, llamapress_version)
    if result is None:
        return {"updates_available": False, "reason": "check_failed"}
    return result


class UpdateRequest(BaseModel):
    llamabot_version: str
    llamapress_version: str


@router.post("/api/update", response_class=JSONResponse)
async def api_perform_update(
    request: UpdateRequest,
    current_user: User = Depends(engineer_or_admin_required),
):
    """Update docker-compose.yml image tags, pull new images, and restart."""
    version_pattern = re.compile(r'^[0-9a-zA-Z.\-]+$')
    if not version_pattern.match(request.llamabot_version) or not version_pattern.match(request.llamapress_version):
        raise HTTPException(status_code=400, detail="Invalid version format")

    from app.routers.slash_commands import execute_command
    command = f"bash bin/update {request.llamabot_version} {request.llamapress_version}"
    try:
        result = execute_command(command, timeout=300)
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip() if result.stdout else "",
            "stderr": result.stderr.strip() if result.stderr else "",
            "return_code": result.returncode,
        }
    except Exception as e:
        # Container may be killed mid-response during restart
        return {"success": True, "stdout": "Update initiated, server restarting...", "stderr": "", "return_code": 0}


# ============== WebSocket Authentication API ==============

@router.get("/api/ws-token", response_class=JSONResponse)
async def get_ws_token(current_user: User = Depends(get_current_user)):
    """
    Generate a JWT token for WebSocket authentication.

    The token is used by the frontend to authenticate WebSocket connections.
    It expires after WS_TOKEN_EXPIRY_MINUTES (default 30 minutes).
    """
    from app.services.token_service import create_ws_token, EXPIRY_MINUTES
    token = create_ws_token(current_user)
    return {"token": token, "expires_in": EXPIRY_MINUTES * 60}


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
                    "created_at": t.created_at.isoformat() + "Z",
                    "updated_at": t.updated_at.isoformat() + "Z",
                    "message_count": t.message_count,
                    "agent_name": t.agent_name
                }
                for t in threads
            ],
            "next_cursor": next_cursor,
            "has_more": has_more
        }


class CompactThreadRequest(BaseModel):
    thread_id: str
    agent_name: str = "rails_agent"


@router.post("/api/compact-thread", response_class=JSONResponse)
async def compact_thread(request_data: CompactThreadRequest, request: Request, username: str = Depends(auth)):
    """Compact the conversation history for a thread by summarizing older messages.

    Mirrors what SummarizationMiddleware does automatically at the token threshold,
    but runs on-demand so the user can trigger it via the /compact slash command.
    Keeps the last 15 messages verbatim and replaces everything before them with a
    structured summary generated by the same summarization model.
    """
    from langchain.agents.middleware import SummarizationMiddleware
    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES
    from app.agents.leonardo.llm_factory import make_summarization_model
    from app.agents.leonardo.rails_agent.nodes import SUMMARIZATION_PROMPT

    app = request.app
    agent_name = request_data.agent_name

    graph = app.state.compiled_graphs.get(agent_name)
    if not graph:
        # All agents share the same MessagesState schema + checkpointer, so any
        # compiled graph can read/write a thread. Fall back to rails_agent → llamabot.
        graph = app.state.compiled_graphs.get("rails_agent") or app.state.compiled_graphs.get("llamabot")
        if not graph:
            raise HTTPException(
                status_code=503,
                detail="No compiled graph available — the server may still be starting up. Try again in a moment."
            )
        logger.info(f"/compact: agent '{agent_name}' not in compiled_graphs, using fallback graph")

    config = {"configurable": {"thread_id": request_data.thread_id}}
    state_snapshot = await graph.aget_state(config=config)

    if not state_snapshot or not state_snapshot.values:
        raise HTTPException(status_code=404, detail="Thread not found or has no messages")

    messages = list(state_snapshot.values.get("messages", []))
    keep_count = 15

    if len(messages) <= keep_count:
        return {
            "success": True,
            "message": f"Only {len(messages)} messages — nothing to compact (need more than {keep_count})",
            "summarized": 0,
            "preserved": len(messages),
        }

    model, token_counter, trim_tokens_to_summarize = make_summarization_model()

    middleware = SummarizationMiddleware(
        model=model,
        trigger=("tokens", 1),  # threshold of 1 ensures it always fires
        keep=("messages", keep_count),
        token_counter=token_counter,
        trim_tokens_to_summarize=trim_tokens_to_summarize,
        summary_prompt=SUMMARIZATION_PROMPT,
    )

    middleware._ensure_message_ids(messages)
    cutoff_index = middleware._determine_cutoff_index(messages)

    if cutoff_index <= 0:
        return {
            "success": True,
            "message": "No messages need to be summarized",
            "summarized": 0,
            "preserved": len(messages),
        }

    messages_to_summarize, preserved_messages = middleware._partition_messages(messages, cutoff_index)
    summary = await middleware._acreate_summary(messages_to_summarize)
    new_messages = middleware._build_new_messages(summary)

    update = {
        "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            *new_messages,
            *preserved_messages,
        ]
    }

    await graph.aupdate_state(config, update)

    logger.info(
        f"User '{username}' compacted thread '{request_data.thread_id}': "
        f"summarized {len(messages_to_summarize)} messages, preserved {len(preserved_messages)}"
    )

    return {
        "success": True,
        "message": (
            f"Compacted {len(messages_to_summarize)} messages into a summary; "
            f"kept {len(preserved_messages)} recent messages verbatim"
        ),
        "summarized": len(messages_to_summarize),
        "preserved": len(preserved_messages),
        "total_before": len(messages),
        "total_after": 1 + len(preserved_messages),
    }


@router.get("/api/thread-tokens/{thread_id}", response_class=JSONResponse)
async def thread_token_count(
    thread_id: str,
    request: Request,
    agent_name: str = Query(default="rails_agent"),
    username: str = Depends(auth),
):
    """Return the tiktoken count of the current checkpoint for a thread.

    Uses the same text-only token counter as SummarizationMiddleware so the
    result matches the threshold that triggers auto-compact.  Counts all message
    content — tool call arguments, tool results, reasoning blocks, etc. — which
    the old client-side character estimate silently missed.  No external API call:
    tiktoken is local and synchronous, so this endpoint is cheap to call on every
    thread load.
    """
    from app.agents.utils.token_counter import tiktoken_token_counter

    app = request.app
    graph = (
        app.state.compiled_graphs.get(agent_name)
        or app.state.compiled_graphs.get("rails_agent")
        or app.state.compiled_graphs.get("llamabot")
    )
    if not graph:
        return {"token_count": 0, "message_count": 0}

    config = {"configurable": {"thread_id": thread_id}}
    try:
        state_snapshot = await graph.aget_state(config=config)
    except Exception:
        return {"token_count": 0, "message_count": 0}

    if not state_snapshot or not state_snapshot.values:
        return {"token_count": 0, "message_count": 0}

    messages = list(state_snapshot.values.get("messages", []))
    token_count = tiktoken_token_counter(messages) if messages else 0

    return {"token_count": token_count, "message_count": len(messages)}


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
    # Values can be a single string or tuple of strings (checked in order, first found wins)
    model_api_keys = {
        "claude-4.5-haiku": "ANTHROPIC_API_KEY",
        "claude-4.5-sonnet": "ANTHROPIC_API_KEY",
        "gpt-5-mini": "OPENAI_API_KEY",
        "gpt-5-codex": "OPENAI_API_KEY",
        "gemini-3-flash": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "gemini-3-pro": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "gemini-3.1-flash-lite": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "deepseek-v4-flash": "DEEPSEEK_API_KEY",
        "deepseek-v4-pro": "DEEPSEEK_API_KEY",
        "qwen3.7-plus": "ALIBABA_API_KEY",
    }

    models = []
    for model_value, env_vars in model_api_keys.items():
        # Support both single string and tuple of env vars
        if isinstance(env_vars, str):
            env_vars = (env_vars,)

        # Check each env var in order, use first one that has a value
        has_key = False
        checked_var = env_vars[0]  # For error message
        for env_var in env_vars:
            api_key = os.environ.get(env_var, "")
            if api_key and api_key.strip():
                has_key = True
                break

        # Operator/mothership policy can disable a model regardless of its key
        # (see model_policy). A disabled model is greyed out in the dropdown; the
        # real enforcement is in get_llm, since this endpoint is UX only.
        enabled = is_model_enabled(model_value)
        if not enabled:
            reason = "Disabled by administrator"
        elif not has_key:
            reason = f"{checked_var} not configured in .env"
        else:
            reason = None

        models.append({
            "value": model_value,
            "available": has_key and enabled,
            "reason": reason,
            "capabilities": get_model_capabilities(model_value),
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

    try:
        file_path = capture_rails_logs(duration=10)
        logs = Path(file_path).read_text()
        return {"logs": logs, "file_path": file_path}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "detail": f"Failed to capture logs: {str(e)}"}
        )


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


# ============== LEONARDO.md API ==============

class UpdateLeonardoMdRequest(BaseModel):
    content: str


@router.get("/api/leonardo-md", response_class=JSONResponse)
async def get_leonardo_md(username: str = Depends(auth)):
    """Get LEONARDO.md content if it exists."""
    leonardo_md_path = ".leonardo/LEONARDO.md"

    if not os.path.exists(leonardo_md_path):
        return {"content": None, "exists": False}

    try:
        with open(leonardo_md_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content, "exists": True}
    except Exception as e:
        logger.warning(f"Error reading LEONARDO.md: {e}")
        return {"content": None, "exists": False, "error": str(e)}


@router.put("/api/leonardo-md", response_class=JSONResponse)
async def update_leonardo_md(
    request: UpdateLeonardoMdRequest,
    current_user: User = Depends(engineer_or_admin_required)
):
    """Update LEONARDO.md content (engineer/admin only)."""
    leonardo_dir = ".leonardo"
    leonardo_md_path = f"{leonardo_dir}/LEONARDO.md"

    # Ensure .leonardo directory exists
    os.makedirs(leonardo_dir, exist_ok=True)

    try:
        with open(leonardo_md_path, "w", encoding="utf-8") as f:
            f.write(request.content)

        logger.info(f"User '{current_user.username}' updated LEONARDO.md")
        return {"message": "LEONARDO.md updated successfully"}
    except Exception as e:
        logger.error(f"Error writing LEONARDO.md: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to write file: {e}")


# ============== Visible Agents Configuration ==============

# Default visible agents for users without a custom configuration
DEFAULT_VISIBLE_AGENTS = ["ticket", "engineer", "testing", "feedback", "user", "beginner"]

# All valid agent mode keys (must match config.js agentModes)
VALID_AGENT_MODES = ["ticket", "engineer", "feedback", "ai_builder", "testing", "user", "beginner"]


class UpdateVisibleAgentsRequest(BaseModel):
    visible_agents: list[str]


@router.get("/api/user/visible-agents", response_class=JSONResponse)
async def get_visible_agents(
    current_user: User = Depends(get_current_user)
):
    """Get current user's visible agents list."""
    if current_user.visible_agents:
        try:
            agents = json.loads(current_user.visible_agents)
            return {"visible_agents": agents}
        except json.JSONDecodeError:
            pass
    return {"visible_agents": DEFAULT_VISIBLE_AGENTS}


@router.put("/api/user/visible-agents", response_class=JSONResponse)
async def set_visible_agents(
    request: UpdateVisibleAgentsRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session)
):
    """Set current user's visible agents list."""
    from datetime import datetime, timezone

    # Validate agent keys
    invalid = [a for a in request.visible_agents if a not in VALID_AGENT_MODES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid agent modes: {invalid}")

    if len(request.visible_agents) == 0:
        raise HTTPException(status_code=400, detail="Must have at least one visible agent")

    current_user.visible_agents = json.dumps(request.visible_agents)
    current_user.updated_at = datetime.now(timezone.utc)
    session.add(current_user)
    session.commit()

    logger.info(f"User '{current_user.username}' updated visible_agents to {request.visible_agents}")
    return {"visible_agents": request.visible_agents, "message": "Visible agents updated"}


# ============== Site Settings API ==============

VALID_SITE_SETTINGS = {"show_token_wheel", "proactive_build_after_ticket", "enable_browser_inspect"}


def get_site_setting(session: Session, key: str, default: str = "false") -> str:
    """Get a site setting value, returning default if not found.

    Falls back to the default when the auth database is unavailable
    (e.g. LEONARDO_DB_URI is not set), matching db.py's degradation.
    """
    from app.models import SiteSetting
    try:
        setting = session.get(SiteSetting, key)
    except Exception as e:
        logger.warning(f"Could not read site setting '{key}', using default '{default}': {e}")
        return default
    return setting.value if setting else default


@router.get("/api/site-settings/{key}", response_class=JSONResponse)
async def api_get_site_setting(
    key: str,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session),
):
    """Get a site setting value."""
    if key not in VALID_SITE_SETTINGS:
        raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")
    value = get_site_setting(session, key)
    return {"key": key, "value": value}


@router.put("/api/site-settings/{key}", response_class=JSONResponse)
async def api_set_site_setting(
    key: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    """Set a site setting value (engineer or admin only)."""
    if current_user.role not in ("engineer",) and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only engineers or admins can change site settings")
    if key not in VALID_SITE_SETTINGS:
        raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")

    from app.models import SiteSetting
    from datetime import datetime, timezone

    body = await request.json()
    value = str(body.get("value", "false"))

    setting = session.get(SiteSetting, key)
    if setting:
        setting.value = value
        setting.updated_at = datetime.now(timezone.utc)
    else:
        setting = SiteSetting(key=key, value=value)
        session.add(setting)
    session.commit()

    logger.info(f"Site setting '{key}' set to '{value}' by {current_user.username}")
    return {"key": key, "value": value}


# ============== Skills Library API ==============

@router.get("/api/skills", response_class=JSONResponse)
async def api_get_skills(
    username: str = Depends(auth),
    session: Session = Depends(get_db_session),
    group: Optional[str] = Query(None, description="Filter by group"),
    search: Optional[str] = Query(None, description="Search term")
):
    """Get all skills, optionally filtered by group or search term."""
    from app.services.skill_service import (
        get_all_skills, get_skills_by_group, search_skills
    )

    if search:
        skills = search_skills(session, search)
    elif group:
        skills = get_skills_by_group(session, group)
    else:
        skills = get_all_skills(session)

    return [
        {
            "id": s.id,
            "name": s.name,
            "content": s.content,
            "description": s.description,
            "group": s.group,
            "usage_count": s.usage_count,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in skills
    ]


@router.get("/api/skills/groups", response_class=JSONResponse)
async def api_get_skill_groups(
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Get list of unique skill groups."""
    from app.services.skill_service import get_skill_groups
    groups = get_skill_groups(session)
    return {"groups": groups}


@router.get("/api/skills/{skill_id}", response_class=JSONResponse)
async def api_get_skill(
    skill_id: int,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Get a specific skill by ID."""
    from app.services.skill_service import get_skill_by_id
    skill = get_skill_by_id(session, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    return {
        "id": skill.id,
        "name": skill.name,
        "content": skill.content,
        "description": skill.description,
        "group": skill.group,
        "usage_count": skill.usage_count,
        "created_at": skill.created_at.isoformat() if skill.created_at else None,
        "updated_at": skill.updated_at.isoformat() if skill.updated_at else None,
    }


@router.post("/api/skills", response_class=JSONResponse)
async def api_create_skill(
    request: CreateSkillRequest,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Create a new skill."""
    from app.services.skill_service import create_skill

    if not request.name or not request.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    if not request.content or not request.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")

    skill = create_skill(
        session,
        name=request.name,
        content=request.content,
        group=request.group,
        description=request.description
    )

    return {
        "id": skill.id,
        "name": skill.name,
        "message": "Skill created successfully"
    }


@router.patch("/api/skills/{skill_id}", response_class=JSONResponse)
async def api_update_skill(
    skill_id: int,
    request: UpdateSkillRequest,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Update an existing skill."""
    from app.services.skill_service import update_skill

    skill = update_skill(
        session, skill_id,
        name=request.name,
        content=request.content,
        group=request.group,
        description=request.description,
        is_active=request.is_active
    )

    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    return {"message": "Skill updated successfully"}


@router.delete("/api/skills/{skill_id}", response_class=JSONResponse)
async def api_delete_skill(
    skill_id: int,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session),
    hard_delete: bool = Query(False, description="Permanently delete")
):
    """Delete a skill (soft delete by default)."""
    from app.services.skill_service import delete_skill

    if not delete_skill(session, skill_id, hard_delete=hard_delete):
        raise HTTPException(status_code=404, detail="Skill not found")

    return {"message": "Skill deleted successfully"}


@router.post("/api/skills/{skill_id}/use", response_class=JSONResponse)
async def api_use_skill(
    skill_id: int,
    username: str = Depends(auth),
    session: Session = Depends(get_db_session)
):
    """Increment usage count when a skill is selected."""
    from app.services.skill_service import increment_usage

    skill = increment_usage(session, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    return {"usage_count": skill.usage_count}


# ============== File Upload to Assets ==============

UPLOAD_ALLOWED_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg',
    # Spreadsheets — all Excel variants, incl. macro-enabled and binary
    '.xlsx', '.xls', '.xlsm', '.xlsb', '.xltx', '.xltm', '.csv',
    # Documents
    '.pdf', '.docx',
    # Slideshows — PowerPoint (incl. macros), Keynote, OpenDocument
    '.pptx', '.ppt', '.pptm', '.key', '.odp',
    # Media
    '.mp4', '.webm',
    # Data / text formats
    '.xml', '.json', '.txt', '.md', '.yaml', '.yml', '.html', '.htm',
    # Archives
    '.zip',
}

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}

# Types a browser can safely render inline. Deliberately excludes SVG: an SVG can
# embed <script>, so serving it inline is an XSS vector — it downloads instead.
# Everything not listed here (Office docs, slideshows, etc.) also downloads; we do
# not render those server-side (no LibreOffice/conversion — keeps the image small
# and the surface area tiny). The OS opens them in the real app.
INLINE_PREVIEW_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf'}

RAILS_ROOT = "/app/app/rails"
IMAGES_DIR = f"{RAILS_ROOT}/app/assets/images"
IMPORTS_DIR = f"{RAILS_ROOT}/app/imports"


@router.post("/api/upload-to-assets", response_class=JSONResponse)
async def upload_to_assets(
    file: UploadFile = File(...),
    username: str = Depends(auth),
):
    """Upload a file: images go to app/assets/images, everything else to app/imports."""
    import pathlib

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate extension
    ext = pathlib.Path(file.filename).suffix.lower()
    if ext not in UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not allowed. Allowed: {', '.join(sorted(UPLOAD_ALLOWED_EXTENSIONS))}"
        )

    # Sanitize filename - keep only safe characters
    safe_filename = re.sub(r'[^\w\-.]', '_', file.filename)

    # Route images to assets/images, everything else to app/imports
    if ext in IMAGE_EXTENSIONS:
        dest_dir = IMAGES_DIR
        relative_path = f"app/assets/images/{safe_filename}"
    else:
        dest_dir = IMPORTS_DIR
        relative_path = f"app/imports/{safe_filename}"

    # Ensure directory exists
    os.makedirs(dest_dir, exist_ok=True)

    # Save file
    dest_path = os.path.join(dest_dir, safe_filename)
    contents = await file.read()
    with open(dest_path, "wb") as f:
        f.write(contents)

    logger.info(f"File uploaded: {relative_path} by {username} ({len(contents)} bytes)")

    return {
        "filename": safe_filename,
        "path": relative_path,
        "size": len(contents),
    }


@router.get("/api/uploaded-files", response_class=JSONResponse)
async def list_uploaded_files(username: str = Depends(auth)):
    """List files in app/assets/images and app/imports."""
    import pathlib

    files = []
    for dir_path, rel_prefix in [(IMAGES_DIR, "app/assets/images"), (IMPORTS_DIR, "app/imports")]:
        d = pathlib.Path(dir_path)
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file() and not f.name.startswith('.'):
                stat = f.stat()
                files.append({
                    "filename": f.name,
                    "path": f"{rel_prefix}/{f.name}",
                    "size": stat.st_size,
                    "uploaded_at": stat.st_mtime,
                    "folder": rel_prefix,
                })

    files.sort(key=lambda x: x["uploaded_at"], reverse=True)
    return {"files": files}


MAX_PREVIEW_BYTES = 50 * 1024 * 1024

PREVIEW_PATH_PREFIXES = {
    "app/assets/images": IMAGES_DIR,
    "app/imports": IMPORTS_DIR,
}


@router.get("/api/uploaded-files/preview")
async def preview_uploaded_file(path: str, download: bool = False, username: str = Depends(auth)):
    """Serve an uploaded file.

    download=1 always forces a download (Content-Disposition: attachment).
    Otherwise we serve inline only for browser-native types (raster images, PDF);
    everything else — Office docs, slideshows, SVG — downloads. Capped at 50MB.
    """
    import pathlib
    from fastapi.responses import FileResponse

    base_dir = None
    filename = None
    for prefix, candidate_base in PREVIEW_PATH_PREFIXES.items():
        if path.startswith(prefix + "/"):
            filename = path[len(prefix) + 1:]
            base_dir = candidate_base
            break
    if base_dir is None or not filename:
        raise HTTPException(status_code=400, detail="Invalid path")

    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")

    real_full = os.path.realpath(os.path.join(base_dir, filename))
    real_base = os.path.realpath(base_dir)
    if not (real_full == real_base or real_full.startswith(real_base + os.sep)):
        raise HTTPException(status_code=400, detail="Path traversal blocked")

    if not os.path.isfile(real_full):
        raise HTTPException(status_code=404, detail="File not found")

    size = os.path.getsize(real_full)
    if size > MAX_PREVIEW_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large to preview ({size} bytes, max {MAX_PREVIEW_BYTES})")

    ext = pathlib.Path(filename).suffix.lower()
    inline = (not download) and ext in INLINE_PREVIEW_EXTENSIONS
    disposition = "inline" if inline else "attachment"
    return FileResponse(real_full, filename=filename, content_disposition_type=disposition)


# ============== Remote Setup API ==============
# Used by the mothership (Rails) to push context files after claiming an instance.
# Auth: HTTP Basic Auth with the admin credentials set up by LlamabotAdminRegistrar.


class WriteFileEntry(BaseModel):
    path: str
    content: str


class WriteFilesRequest(BaseModel):
    files: list[WriteFileEntry]


# Map friendly mothership paths to actual filesystem paths.
# Paths not in this map are written as-is (relative to CWD).
WRITE_FILES_PATH_MAP = {
    "Leonardo.md": ".leonardo/LEONARDO.md",
    "User.md": ".leonardo/USER.md",
}


@router.post("/api/write_files", response_class=JSONResponse)
async def api_write_files(
    body: WriteFilesRequest,
    current_user: User = Depends(admin_required),
):
    """Write files to disk. Used by the mothership to push context files after claiming an instance."""
    results = []

    for entry in body.files:
        file_path = entry.path

        # Resolve friendly names to actual paths
        resolved = WRITE_FILES_PATH_MAP.get(file_path, file_path)
        resolved = os.path.normpath(resolved)

        # Prevent path traversal outside the working directory
        if resolved.startswith("..") or os.path.isabs(resolved):
            results.append({"path": file_path, "status": "error", "detail": "Absolute or traversal paths not allowed"})
            continue

        try:
            parent = os.path.dirname(resolved)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(entry.content)
            logger.info(f"write_files: wrote {resolved} ({len(entry.content)} chars) by {current_user.username}")
            results.append({"path": file_path, "status": "ok"})
        except Exception as e:
            logger.error(f"write_files: failed to write {file_path}: {e}")
            results.append({"path": file_path, "status": "error", "detail": str(e)})

    return {"results": results}


class ImportFromS3Request(BaseModel):
    url: str
    filename: str | None = None


@router.post("/api/setup/import-excel", response_class=JSONResponse)
async def api_import_excel_from_s3(
    body: ImportFromS3Request,
    current_user: User = Depends(admin_required),
):
    """Download a file from S3 (pre-signed URL) and save it to rails/app/imports/."""
    import pathlib
    import httpx

    # Derive filename from the URL if not provided (strip query params)
    if body.filename:
        filename = body.filename
    else:
        url_path = body.url.split("?")[0]
        filename = url_path.rsplit("/", 1)[-1]

    if not filename:
        raise HTTPException(status_code=400, detail="Could not determine filename from URL. Provide a 'filename' field.")

    # Validate extension
    ext = pathlib.Path(filename).suffix.lower()
    if ext not in {".xlsx", ".xls", ".csv"}:
        raise HTTPException(status_code=400, detail=f"File type '{ext}' not allowed. Allowed: .xlsx, .xls, .csv")

    # Sanitize filename
    safe_filename = re.sub(r'[^\w\-.]', '_', filename)

    os.makedirs(IMPORTS_DIR, exist_ok=True)
    dest_path = os.path.join(IMPORTS_DIR, safe_filename)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(body.url)
            resp.raise_for_status()

        with open(dest_path, "wb") as f:
            f.write(resp.content)

        logger.info(f"import-excel: downloaded {safe_filename} ({len(resp.content)} bytes) by {current_user.username}")
        return {
            "filename": safe_filename,
            "path": f"app/imports/{safe_filename}",
            "size": len(resp.content),
        }
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Failed to download from S3: HTTP {e.response.status_code}")
    except Exception as e:
        logger.error(f"import-excel: failed to download {body.url}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to download file: {e}")


ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_IMAGE_SIZE = 25 * 1024 * 1024  # 25 MB


@router.post("/api/setup/import-image", response_class=JSONResponse)
async def api_import_image_from_s3(
    body: ImportFromS3Request,
    current_user: User = Depends(admin_required),
):
    """Download an image from S3 and save it to rails/app/imports/."""
    import pathlib
    import httpx
    from urllib.parse import unquote

    # Derive filename
    if body.filename:
        filename = body.filename
    else:
        url_path = body.url.split("?")[0]
        filename = unquote(url_path.rsplit("/", 1)[-1])

    if not filename:
        raise HTTPException(status_code=400, detail="Could not determine filename from URL. Provide a 'filename' field.")

    # Validate extension
    ext = pathlib.Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"not an image: {ext}")

    # Sanitize (strip path traversal, unsafe chars)
    safe_filename = re.sub(r'[^\w\-.]', '_', os.path.basename(filename))

    os.makedirs(IMPORTS_DIR, exist_ok=True)
    dest_path = os.path.join(IMPORTS_DIR, safe_filename)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(body.url)
            resp.raise_for_status()

        # Size check
        if len(resp.content) > MAX_IMAGE_SIZE:
            raise HTTPException(status_code=400, detail=f"Image too large: {len(resp.content)} bytes (max {MAX_IMAGE_SIZE})")

        # Magic-byte validation
        header = resp.content[:12]
        is_valid_image = (
            header[:8] == b'\x89PNG\r\n\x1a\n'
            or header[:2] == b'\xff\xd8'
            or header[:4] == b'GIF8'
            or (header[:4] == b'RIFF' and header[8:12] == b'WEBP')
        )
        if not is_valid_image:
            raise HTTPException(status_code=400, detail="File content does not match a supported image format")

        with open(dest_path, "wb") as f:
            f.write(resp.content)

        logger.info(f"import-image: downloaded {safe_filename} ({len(resp.content)} bytes) by {current_user.username}")
        return {"status": "ok", "saved_path": f"rails/app/imports/{safe_filename}"}

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Failed to download from S3: HTTP {e.response.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"import-image: failed to download {body.url}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to download file: {e}")
