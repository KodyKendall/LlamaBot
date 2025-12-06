from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import os
import logging
import json
import asyncio
import secrets
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session
from app.db import init_db, engine, get_session
from app.models import User
from app.services.user_service import (
    authenticate_user, create_user, get_user_by_username,
    get_all_users, get_user_by_id, update_user, delete_user
)
from app.services.auth_migration import migrate_auth_json

from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import ConnectionPool
from pydantic import BaseModel
from dotenv import load_dotenv

from langchain_core.load import dumpd
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver

from app.agents.llamabot.nodes import build_workflow
from app.websocket.web_socket_connection_manager import WebSocketConnectionManager
from app.websocket.web_socket_handler import WebSocketHandler
from app.websocket.request_handler import RequestHandler
from collections import defaultdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('chat_app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI()

# Add CORS middleware for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for ngrok/external access
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static directories
frontend_dir = Path(__file__).parent / "frontend"
app.mount("/frontend", StaticFiles(directory=str(frontend_dir)), name="frontend")

# This is responsible for holding and managing all active websocket connections.
manager = WebSocketConnectionManager(app) 

# Application state to hold persistent checkpointer, important for session-based persistence.
app.state.checkpointer = None
app.state.async_checkpointer = None
app.state.timestamp = datetime.now(timezone.utc)
app.state.compiled_graphs = {}  # Cache for pre-compiled LangGraph workflows

# Initialize HTTP Basic Auth
security = HTTPBasic()

# Path to legacy auth file (for migration)
LEGACY_AUTH_FILE = Path(__file__).parent / "auth.json"

# Suppress psycopg connection error spam when PostgreSQL is unavailable
psycopg_logger = logging.getLogger('psycopg.pool')
psycopg_logger.setLevel(logging.ERROR)

def get_or_create_checkpointer():
    """Get persistent checkpointer, creating once if needed"""
    if app.state.checkpointer is None:
        db_uri = os.getenv("DB_URI")
        if db_uri and db_uri.strip():
            try:
                # Create connection pool with limited retries and timeout
                # Reduced pool size for single worker (was max_size=5)
                pool = ConnectionPool(
                    db_uri,
                    min_size=1,
                    max_size=2,  # Reduced from 5 (single worker doesn't need many)
                    timeout=5.0,  # 5 second connection timeout
                    max_idle=300.0,  # 5 minute idle timeout
                    max_lifetime=3600.0,  # 1 hour max connection lifetime
                    reconnect_failed=lambda pool: logger.warning("PostgreSQL connection failed, using MemorySaver for persistence")
                )
                app.state.checkpointer = PostgresSaver(pool)
                # app.state.checkpointer.setup()
                logger.info("✅ Connected to PostgreSQL for persistence")
            except Exception as e:
                logger.warning(f"❌ DB_URI: {db_uri}")
                logger.warning(f"❌ PostgreSQL unavailable ({str(e).split(':', 1)[0]}). Using MemorySaver for session-based persistence.")
                app.state.checkpointer = MemorySaver()
        else:
            logger.info("📝 No DB_URI configured. Using MemorySaver for session-based persistence.")
            app.state.checkpointer = MemorySaver()
    
    return app.state.checkpointer

def get_or_create_async_checkpointer():
    """Get async persistent checkpointer, creating once if needed"""
    if app.state.async_checkpointer is None:
        db_uri = os.getenv("DB_URI")
        if db_uri and db_uri.strip():
            try:
                # Create async connection pool with limited retries and timeout
                # Reduced pool size for single worker (was max_size=5)
                pool = AsyncConnectionPool(
                    db_uri,
                    min_size=1,
                    max_size=2,  # Reduced from 5 (single worker doesn't need many)
                    timeout=5.0,  # 5 second connection timeout
                    max_idle=300.0,  # 5 minute idle timeout
                    max_lifetime=3600.0,  # 1 hour max connection lifetime
                    reconnect_failed=lambda pool: logger.warning("PostgreSQL async connection failed, using MemorySaver for persistence")
                )
                app.state.async_checkpointer = AsyncPostgresSaver(pool)
                # app.state.async_checkpointer.setup()
                logger.info("✅ Connected to PostgreSQL (async) for persistence")
            except Exception as e:
                logger.warning(f"❌ DB_URI: {db_uri}")
                logger.warning(f"❌ PostgreSQL unavailable for async operations ({str(e).split(':', 1)[0]}). Using MemorySaver for session-based persistence.")
                app.state.async_checkpointer = MemorySaver()
        else:
            logger.info("📝 No DB_URI configured. Using MemorySaver for async session-based persistence.")
            app.state.async_checkpointer = MemorySaver()
    
    return app.state.async_checkpointer

def get_langgraph_app_and_state_helper(message: dict):
    """Helper function to access RequestHandler.get_langgraph_app_and_state from main.py"""
    request_handler = RequestHandler(app)
    response = request_handler.get_langgraph_app_and_state(message)
    return response

def get_db_session():
    """Get database session for dependency injection."""
    with Session(engine) as session:
        yield session


def auth(
    credentials: HTTPBasicCredentials = Depends(security),
    session: Session = Depends(get_db_session)
) -> str:
    """Validate HTTP Basic Auth credentials against database."""
    user = authenticate_user(session, credentials.username, credentials.password)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return user.username


def get_current_user(
    credentials: HTTPBasicCredentials = Depends(security),
    session: Session = Depends(get_db_session)
) -> User:
    """Get the current authenticated user object."""
    user = authenticate_user(session, credentials.username, credentials.password)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return user


def admin_required(
    current_user: User = Depends(get_current_user)
) -> User:
    """Require admin privileges."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Admin privileges required"
        )
    return current_user

# At module level
thread_locks = defaultdict(asyncio.Lock)
thread_queues = defaultdict(asyncio.Queue)
MAX_QUEUE_SIZE = 10

@app.get("/", response_class=HTMLResponse)
async def root(current_user: User = Depends(get_current_user)):
    # Serve the chat.html file with user role injected
    with open(frontend_dir / "chat.html") as f:
        html = f.read()
    # Inject user role as a global variable for the frontend
    role_script = f'<script>window.LLAMABOT_USER_ROLE = "{getattr(current_user, "role", "engineer")}";</script>'
    html = html.replace('</head>', f'{role_script}</head>')
    return html
    
@app.get("/hello", response_class=JSONResponse)
async def hello():
    return {"message": "Hello, World! 🦙💬"}

@app.get("/register", response_class=HTMLResponse)
async def register_form():
    """Serve the registration form"""
    with open("register.html") as f:
        html = f.read()
        return HTMLResponse(content=html)

@app.post("/register")
async def register(
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    session: Session = Depends(get_db_session)
):
    """Process registration form - creates new user in database."""
    # Validate passwords match
    if password != confirm:
        raise HTTPException(
            status_code=400,
            detail="Passwords do not match"
        )

    # Check if username already exists
    existing_user = get_user_by_username(session, username)
    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Username already exists"
        )

    try:
        user = create_user(session, username, password)
        logger.info(f"User '{username}' registered successfully")
        return JSONResponse({
            "message": f"User '{username}' registered successfully! You can now use these credentials to access protected endpoints."
        })
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to create user"
        )


# ============== Admin User Management ==============

@app.get("/users", response_class=HTMLResponse)
async def users_page(admin: User = Depends(admin_required)):
    """Serve the admin user management page."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot User Management</title>
    <link rel="icon" type="image/png" href="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {
            --bg-color: #1a1a1a;
            --chat-bg: #2d2d2d;
            --text-color: #e0e0e0;
            --border-color: #404040;
            --accent-color: #4CAF50;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }
        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        .header {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 30px;
        }
        .back-btn {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            text-decoration: none;
            transition: background 0.2s;
        }
        .back-btn:hover { background: var(--border-color); }
        h1 { font-size: 1.5rem; margin: 0; }
        .card {
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }
        .card-header {
            font-size: 0.85rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
        }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid var(--border-color); }
        th { color: #888; font-weight: 500; font-size: 0.85rem; }
        tr:last-child td { border-bottom: none; }
        .badge {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 500;
            text-transform: uppercase;
        }
        .badge-engineer { background: rgba(33, 150, 243, 0.2); color: #64b5f6; }
        .badge-user { background: rgba(156, 39, 176, 0.2); color: #ce93d8; }
        .badge-admin { background: rgba(76, 175, 80, 0.2); color: #81c784; }
        .badge-active { background: rgba(76, 175, 80, 0.2); color: #81c784; }
        .badge-inactive { background: rgba(244, 67, 54, 0.2); color: #e57373; }
        .btn {
            padding: 6px 12px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            margin-right: 4px;
            background: transparent;
            color: var(--text-color);
            transition: all 0.2s;
        }
        .btn:hover { background: var(--border-color); }
        .btn-danger { border-color: #e57373; color: #e57373; }
        .btn-danger:hover { background: rgba(244, 67, 54, 0.2); }
        .form-row { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; }
        .form-group { flex: 1; min-width: 120px; }
        .form-group label { display: block; margin-bottom: 6px; font-size: 0.85rem; color: #888; }
        .form-group input, .form-group select {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            box-sizing: border-box;
        }
        .form-group input:focus, .form-group select:focus {
            outline: none;
            border-color: var(--accent-color);
        }
        .btn-primary {
            background: var(--accent-color);
            border-color: var(--accent-color);
            color: white;
            padding: 10px 20px;
        }
        .btn-primary:hover { opacity: 0.9; background: var(--accent-color); }
        .message {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: none;
        }
        .message.success { background: rgba(76, 175, 80, 0.2); color: #81c784; display: block; }
        .message.error { background: rgba(244, 67, 54, 0.2); color: #e57373; display: block; }
        .actions { white-space: nowrap; }
        select.role-select {
            padding: 4px 8px;
            font-size: 12px;
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            border: 1px solid var(--border-color);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/settings" class="back-btn">
                <i class="fa-solid fa-arrow-left"></i>
            </a>
            <h1>User Management</h1>
        </div>

        <div id="message" class="message"></div>

        <div class="card">
            <div class="card-header">Add New User</div>
            <form id="addUserForm" class="form-row">
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" id="newUsername" required>
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" id="newPassword" required>
                </div>
                <div class="form-group">
                    <label>Role</label>
                    <select id="newRole">
                        <option value="engineer">Engineer</option>
                        <option value="user">User</option>
                    </select>
                </div>
                <button type="submit" class="btn btn-primary">Add User</button>
            </form>
        </div>

        <div class="card">
            <div class="card-header">Users</div>
            <table>
                <thead>
                    <tr>
                        <th>Username</th>
                        <th>Role</th>
                        <th>Status</th>
                        <th>Created</th>
                        <th class="actions">Actions</th>
                    </tr>
                </thead>
                <tbody id="usersTable">
                    <tr><td colspan="5">Loading...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
        async function loadUsers() {
            try {
                const response = await fetch('/api/users');
                const users = await response.json();
                const tbody = document.getElementById('usersTable');

                if (users.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5">No users found</td></tr>';
                    return;
                }

                tbody.innerHTML = users.map(user => `
                    <tr>
                        <td><strong>${user.username}</strong>${user.is_admin ? ' <span class="badge badge-admin">Admin</span>' : ''}</td>
                        <td>
                            <select class="role-select" onchange="updateRole(${user.id}, this.value)">
                                <option value="engineer" ${user.role === 'engineer' ? 'selected' : ''}>Engineer</option>
                                <option value="user" ${user.role === 'user' ? 'selected' : ''}>User</option>
                            </select>
                        </td>
                        <td>${user.is_active ? '<span class="badge badge-active">Active</span>' : '<span class="badge badge-inactive">Inactive</span>'}</td>
                        <td>${new Date(user.created_at).toLocaleDateString()}</td>
                        <td class="actions">
                            ${user.is_active
                                ? `<button class="btn" onclick="toggleActive(${user.id}, false)">Deactivate</button>`
                                : `<button class="btn" onclick="toggleActive(${user.id}, true)">Activate</button>`
                            }
                            <button class="btn btn-danger" onclick="deleteUser(${user.id}, '${user.username}')">Delete</button>
                        </td>
                    </tr>
                `).join('');
            } catch (error) {
                showMessage('Error loading users: ' + error.message, 'error');
            }
        }

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => msg.className = 'message', 3000);
        }

        async function updateRole(userId, role) {
            try {
                const response = await fetch(`/api/users/${userId}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: role})
                });
                if (response.ok) {
                    showMessage('Role updated', 'success');
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error updating role', 'error');
                    loadUsers();
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        }

        async function toggleActive(userId, isActive) {
            try {
                const response = await fetch(`/api/users/${userId}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({is_active: isActive})
                });
                if (response.ok) {
                    showMessage('User updated', 'success');
                    loadUsers();
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error updating user', 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        }

        async function deleteUser(userId, username) {
            if (!confirm(`Delete user "${username}"?`)) return;
            try {
                const response = await fetch(`/api/users/${userId}`, {method: 'DELETE'});
                if (response.ok) {
                    showMessage('User deleted', 'success');
                    loadUsers();
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error deleting user', 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        }

        document.getElementById('addUserForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const username = document.getElementById('newUsername').value;
            const password = document.getElementById('newPassword').value;
            const role = document.getElementById('newRole').value;

            try {
                const response = await fetch('/api/users', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username, password, role: role})
                });

                if (response.ok) {
                    showMessage('User created', 'success');
                    document.getElementById('addUserForm').reset();
                    loadUsers();
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error creating user', 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        });

        loadUsers();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


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


@app.get("/api/users", response_class=JSONResponse)
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


@app.post("/api/users", response_class=JSONResponse)
async def api_create_user(
    request: CreateUserRequest,
    admin: User = Depends(admin_required),
    session: Session = Depends(get_db_session)
):
    """Create a new user (admin only)."""
    # Check if username exists
    if get_user_by_username(session, request.username):
        raise HTTPException(status_code=409, detail="Username already exists")

    from app.services.user_service import hash_password
    user = User(
        username=request.username,
        password_hash=hash_password(request.password),
        is_admin=request.is_admin,
        role=request.role
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    logger.info(f"Admin '{admin.username}' created user '{request.username}' with role '{request.role}'")
    return {"id": user.id, "username": user.username, "message": "User created successfully"}


@app.patch("/api/users/{user_id}", response_class=JSONResponse)
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


@app.delete("/api/users/{user_id}", response_class=JSONResponse)
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


# ============== Settings & Logout ==============

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(current_user: User = Depends(get_current_user)):
    """Serve the settings page."""
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot Settings</title>
    <link rel="icon" type="image/png" href="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {{
            --bg-color: #1a1a1a;
            --chat-bg: #2d2d2d;
            --text-color: #e0e0e0;
            --border-color: #404040;
            --accent-color: #4CAF50;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            padding: 40px 20px;
        }}
        .header {{
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 40px;
        }}
        .back-btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            width: 40px;
            height: 40px;
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            text-decoration: none;
            transition: background 0.2s;
        }}
        .back-btn:hover {{
            background: var(--border-color);
        }}
        h1 {{
            font-size: 1.5rem;
            margin: 0;
        }}
        .card {{
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }}
        .card-header {{
            font-size: 0.85rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
        }}
        .user-info {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .user-avatar {{
            width: 48px;
            height: 48px;
            background: var(--accent-color);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
            font-weight: 600;
            color: white;
        }}
        .user-details h3 {{
            margin: 0 0 4px 0;
            font-size: 1.1rem;
        }}
        .user-details .role {{
            font-size: 0.85rem;
            color: #888;
        }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            background: rgba(76, 175, 80, 0.2);
            color: var(--accent-color);
            border-radius: 4px;
            font-size: 0.75rem;
            margin-left: 8px;
        }}
        .menu-item {{
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 14px 0;
            border-bottom: 1px solid var(--border-color);
            cursor: pointer;
            transition: opacity 0.2s;
        }}
        .menu-item:last-child {{
            border-bottom: none;
        }}
        .menu-item:hover {{
            opacity: 0.8;
        }}
        .menu-item i {{
            width: 24px;
            text-align: center;
            font-size: 16px;
        }}
        .menu-item span {{
            flex: 1;
        }}
        .menu-item .chevron {{
            color: #666;
        }}
        .logout-btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            width: 100%;
            padding: 14px;
            background: #d32f2f;
            border: none;
            border-radius: 8px;
            color: white;
            font-size: 1rem;
            cursor: pointer;
            transition: background 0.2s;
        }}
        .logout-btn:hover {{
            background: #b71c1c;
        }}
        a {{
            color: inherit;
            text-decoration: none;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/" class="back-btn">
                <i class="fa-solid fa-arrow-left"></i>
            </a>
            <h1>Settings</h1>
        </div>

        <div class="card">
            <div class="card-header">Account</div>
            <div class="user-info">
                <div class="user-avatar">{current_user.username[0].upper()}</div>
                <div class="user-details">
                    <h3>{current_user.username}{"<span class='badge'>Admin</span>" if current_user.is_admin else ""}</h3>
                    <div class="role">{"Administrator" if current_user.is_admin else "User"}</div>
                </div>
            </div>
        </div>

        {"<div class='card'><a href='/users' class='menu-item'><i class='fa-solid fa-users'></i><span>User Management</span><i class='fa-solid fa-chevron-right chevron'></i></a></div>" if current_user.is_admin else ""}

        <div class="card">
            <button class="logout-btn" onclick="logout()">
                <i class="fa-solid fa-right-from-bracket"></i>
                Sign Out
            </button>
        </div>
    </div>

    <script>
        function logout() {{
            // Clear credentials by making a request that will fail, then redirect
            fetch('/logout', {{
                method: 'POST',
                headers: {{
                    'Authorization': 'Basic ' + btoa('logout:logout')
                }}
            }}).finally(() => {{
                // Redirect to home which will prompt for new credentials
                window.location.href = '/?logout=' + Date.now();
            }});
        }}
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@app.post("/logout")
async def logout():
    """
    Logout endpoint - returns 401 to clear browser's cached credentials.
    The browser will prompt for new credentials on the next request.
    """
    raise HTTPException(
        status_code=401,
        detail="Logged out",
        headers={"WWW-Authenticate": "Basic"}
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await WebSocketHandler(websocket, manager).handle_websocket()

@app.on_event("startup")
async def startup_event():
    """Initialize database, migrate users, and compile LangGraph workflows."""
    # Initialize SQLite database
    logger.info("📦 Initializing SQLite database...")
    init_db()

    # Migrate auth.json if it exists
    with Session(engine) as session:
        migrate_auth_json(session, LEGACY_AUTH_FILE)

    # Compile all LangGraph workflows once at startup (singleton pattern)
    logger.info("🔨 Compiling LangGraph workflows...")
    try:
        checkpointer = get_or_create_async_checkpointer()

        # Import all build_workflow functions
        from app.agents.llamabot.nodes import build_workflow as build_llamabot
        from app.agents.llamapress.nodes import build_workflow as build_llamapress
        from app.agents.leonardo.rails_agent.nodes import build_workflow as build_rails_agent

        # Compile once and cache - these are thread-safe singletons
        app.state.compiled_graphs = {
            "llamabot": build_llamabot(checkpointer=checkpointer),
            "llamapress": build_llamapress(checkpointer=checkpointer),
            "rails_agent": build_rails_agent(checkpointer=checkpointer),
        }

        # Optional agents (may not exist in all deployments)
        try:
            from app.agents.leonardo.rails_ai_builder_agent.nodes import build_workflow as build_rails_ai_builder
            app.state.compiled_graphs["rails_ai_builder_agent"] = build_rails_ai_builder(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_ai_builder_agent not found, skipping")

        try:
            from app.agents.leonardo.rails_frontend_starter_agent.nodes import build_workflow as build_rails_frontend
            app.state.compiled_graphs["rails_frontend_starter_agent"] = build_rails_frontend(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_frontend_starter_agent not found, skipping")

        try:
            from app.agents.leonardo.rails_user_feedback_agent.nodes import build_workflow as build_rails_user_feedback
            app.state.compiled_graphs["rails_user_feedback_agent"] = build_rails_user_feedback(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_user_feedback_agent not found, skipping")

        logger.info(f"✅ Compiled {len(app.state.compiled_graphs)} LangGraph workflows: {list(app.state.compiled_graphs.keys())}")
    except Exception as e:
        logger.error(f"❌ Error compiling LangGraph workflows: {e}", exc_info=True)
        # Don't fail startup - fall back to per-request compilation
        app.state.compiled_graphs = {}

    # Log streaming disabled for now
    pass
    # asyncio.create_task(tail_docker_logs())
    # asyncio.create_task(broadcast_logs())
    # logger.info("✅ Docker log streaming started")

@app.get("/conversations", response_class=HTMLResponse)
async def conversations(username: str = Depends(auth)):
    with open("conversations.html") as f:
        return f.read()

@app.get("/threads", response_class=JSONResponse)
async def threads(username: str = Depends(auth)):
    checkpointer = get_or_create_checkpointer()
    config = {}
    checkpoint_generator = checkpointer.list(config=config)

    # ❌ OLD: all_checkpoints = list(checkpoint_generator)  # Loads ALL checkpoints into memory!
    # ✅ NEW: Stream through generator to extract unique thread_ids without loading all data
    unique_thread_ids = set()
    for checkpoint in checkpoint_generator:
        thread_id = checkpoint[0]["configurable"]["thread_id"]
        unique_thread_ids.add(thread_id)

    unique_thread_ids = list(unique_thread_ids)
    logger.info(f"📊 Found {len(unique_thread_ids)} unique threads (memory-efficient extraction)")

    # Optional: Limit threads returned to prevent excessive memory usage
    MAX_THREADS = 100
    if len(unique_thread_ids) > MAX_THREADS:
        logger.warning(f"⚠️ Limiting threads response from {len(unique_thread_ids)} to {MAX_THREADS} threads")
        unique_thread_ids = unique_thread_ids[:MAX_THREADS]

    state_history = []

    # ✅ Use cached graph from startup (singleton pattern)
    graph = app.state.compiled_graphs.get("llamabot")
    if not graph:
        from app.agents.llamabot.nodes import build_workflow
        graph = build_workflow(checkpointer=checkpointer)
        logger.warning("⚠️ /threads endpoint using fallback graph compilation")

    # Get only the LATEST state for each thread (not full history)
    for thread_id in unique_thread_ids:
        config = {"configurable": {"thread_id": thread_id}}
        state_history.append({"thread_id": thread_id, "state": await graph.aget_state(config=config)})

    return state_history

@app.get("/chat-history/{thread_id}")
async def chat_history(thread_id: str, username: str = Depends(auth)):
    checkpointer = get_or_create_checkpointer()

    # ✅ Use cached graph from startup (singleton pattern)
    graph = app.state.compiled_graphs.get("llamabot")
    if not graph:
        # Fallback: compile once if not in cache
        from app.agents.llamabot.nodes import build_workflow
        graph = build_workflow(checkpointer=checkpointer)
        logger.warning("⚠️ /chat-history endpoint using fallback graph compilation")

    config = {"configurable": {"thread_id": thread_id}}
    state_history = await graph.aget_state(config=config) #graph.aget_state returns a StateSnapshot object, which inherits from a Named Tuple. Serializes into an Array.
    print(state_history)
    return state_history

@app.get("/available-agents", response_class=JSONResponse)
async def available_agents():
    # map from langgraph.json to a list of agent names
    with open("langgraph.json", "r") as f:
        langgraph_json = json.load(f)
    return {"agents": list(langgraph_json["graphs"].keys())}

@app.get("/rails-routes", response_class=JSONResponse)
async def rails_routes():
    """Parse routes.rb and return available GET routes for `index` actions and home page"""
    import re
    import os

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

@app.get("/check")
def check_timestamp():
    # returns the timestamp of last message from user in utc
    return {"timestamp": app.state.timestamp}
    """Stream real-time Rails logs via Docker to browser using SSE"""

    async def log_generator():
        # Create queue for this client
        client_queue = asyncio.Queue(maxsize=500)
        clients.add(client_queue)

        try:
            # Send initial connection message
            yield f"data: {json.dumps({'type': 'connected', 'sources': ['rails']})}\n\n"

            # Stream logs as they arrive from broadcast
            while True:
                try:
                    log_entry = await asyncio.wait_for(client_queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(log_entry)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive to prevent connection timeout
                    yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

        except asyncio.CancelledError:
            logger.info("Log streaming cancelled by client")
        except Exception as e:
            logger.error(f"Error in log stream: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            # Cleanup: remove client queue
            clients.discard(client_queue)
            logger.info(f"Client disconnected. Active clients: {len(clients)}")

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

# Mount MCP Server (stubbed out for now - MCP not yet fully implemented)
# from app.mcp_server import mcp
# app.mount("/mcp", mcp.sse_app(mount_path=""))