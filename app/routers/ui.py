"""UI/HTML page routes for LlamaBot."""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session

from app.db import engine
from app.models import User
from app.dependencies import (
    security, get_db_session, auth, get_current_user, admin_required, has_any_users,
    engineer_or_admin_required
)
from app.services.user_service import authenticate_user, get_user_by_username

# Role-based default visible agents
DEFAULT_VISIBLE_AGENTS_USER = ["feedback"]
DEFAULT_VISIBLE_AGENTS_ENGINEER = ["ticket", "engineer", "testing", "feedback", "user", "prototype", "ai_builder", "architect"]

logger = logging.getLogger(__name__)

router = APIRouter()

# Frontend directory path
frontend_dir = Path(__file__).parent.parent / "frontend"


@router.get("/")
async def root(request: Request):
    # If no users exist, redirect to registration
    if not has_any_users():
        return RedirectResponse(url="/register", status_code=302)

    # Otherwise require authentication
    credentials = await security(request)
    with Session(engine) as session:
        user = authenticate_user(session, credentials.username, credentials.password)
        if not user:
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )

        # Get visible agents for this user (role-based defaults)
        visible_agents = None
        if user.visible_agents:
            try:
                visible_agents = json.loads(user.visible_agents)
            except json.JSONDecodeError:
                pass

        # If no custom setting, use role-based defaults
        if not visible_agents:
            if getattr(user, "role", "engineer") == "user":
                visible_agents = DEFAULT_VISIBLE_AGENTS_USER
            else:
                visible_agents = DEFAULT_VISIBLE_AGENTS_ENGINEER

        # Serve the chat.html file with user role and visible agents injected
        with open(frontend_dir / "chat.html") as f:
            html = f.read()
        # Inject user role and visible agents as global variables for the frontend
        config_script = f'''<script>
window.LLAMABOT_USER_ROLE = "{getattr(user, "role", "engineer")}";
window.LLAMABOT_VISIBLE_AGENTS = {json.dumps(visible_agents)};
</script>'''
        html = html.replace('</head>', f'{config_script}</head>')
        return HTMLResponse(content=html)


@router.get("/hello", response_class=JSONResponse)
async def hello():
    return {"message": "Hello, World! 🦙💬"}


@router.get("/register", response_class=HTMLResponse)
async def register_form():
    """Serve the registration form - only available when no users exist."""
    # Security: Only allow registration if no users exist
    if has_any_users():
        raise HTTPException(
            status_code=403,
            detail="Registration is disabled. Please contact an administrator to create an account."
        )

    with open("register.html") as f:
        html = f.read()
        return HTMLResponse(content=html)


@router.post("/register")
async def register(
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    session: Session = Depends(get_db_session)
):
    """Process registration form - only works when no users exist (creates first admin user)."""
    # Security: Only allow registration if no users exist
    if has_any_users():
        raise HTTPException(
            status_code=403,
            detail="Registration is disabled. Please contact an administrator to create an account."
        )

    # Validate passwords match
    if password != confirm:
        raise HTTPException(
            status_code=400,
            detail="Passwords do not match"
        )

    # Check if username already exists (shouldn't happen if no users, but be safe)
    existing_user = get_user_by_username(session, username)
    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Username already exists"
        )

    try:
        # First user is always an admin
        from app.services.user_service import create_admin_user
        user = create_admin_user(session, username, password)
        logger.info(f"Admin user '{username}' registered successfully (first user)")
        return JSONResponse({
            "message": f"Admin user '{username}' registered successfully! You can now use these credentials to access the app."
        })
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to create user"
        )


@router.get("/users", response_class=HTMLResponse)
async def users_page(admin: User = Depends(admin_required)):
    """Serve the admin user management page."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot User Management</title>
    <link rel="icon" type="image/png" href="https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/4bmqe5iolvp84ceyk9ttz8vylrym">
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


@router.get("/prompt-library", response_class=HTMLResponse)
async def prompt_library_page(current_user: User = Depends(get_current_user)):
    """Serve the prompt library management page."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot Prompt Library</title>
    <link rel="icon" type="image/png" href="https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/4bmqe5iolvp84ceyk9ttz8vylrym">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root {
            --bg-color: #1a1a1a;
            --chat-bg: #2d2d2d;
            --text-color: #e0e0e0;
            --border-color: #404040;
            --accent-color: #8b5cf6;
            --accent-hover: #7c3aed;
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
            max-width: 1000px;
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
        h1 { font-size: 1.5rem; margin: 0; flex: 1; }
        .card {
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }
        .prompt-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 16px;
        }
        .prompt-card {
            background: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .prompt-card:hover {
            border-color: var(--accent-color);
            transform: translateY(-2px);
        }
        .prompt-name {
            font-weight: 600;
            margin-bottom: 8px;
            color: var(--text-color);
        }
        .prompt-description {
            font-size: 0.8rem;
            color: rgba(255,255,255,0.5);
            margin-bottom: 8px;
            font-style: italic;
        }
        .prompt-content {
            font-size: 0.85rem;
            color: rgba(255,255,255,0.6);
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .prompt-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 12px;
            font-size: 0.75rem;
            color: rgba(255,255,255,0.4);
        }
        .prompt-group-badge {
            background: rgba(139, 92, 246, 0.2);
            color: var(--accent-color);
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.7rem;
        }
        .btn {
            padding: 8px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85rem;
            background: transparent;
            color: var(--text-color);
            transition: all 0.2s;
        }
        .btn:hover { background: var(--border-color); }
        .btn-primary {
            background: var(--accent-color);
            border-color: var(--accent-color);
            color: white;
        }
        .btn-primary:hover { background: var(--accent-hover); }
        .btn-danger { border-color: #e57373; color: #e57373; }
        .btn-danger:hover { background: rgba(244, 67, 54, 0.2); }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; margin-bottom: 6px; font-size: 0.85rem; color: #888; }
        .form-group input, .form-group select, .form-group textarea {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            box-sizing: border-box;
            font-family: inherit;
        }
        .form-group textarea { min-height: 150px; resize: vertical; }
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
            outline: none;
            border-color: var(--accent-color);
        }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.7);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: var(--chat-bg);
            border-radius: 12px;
            padding: 24px;
            max-width: 600px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .modal-header h2 { margin: 0; font-size: 1.2rem; }
        .modal-close {
            background: none;
            border: none;
            font-size: 1.5rem;
            cursor: pointer;
            color: var(--text-color);
            opacity: 0.6;
        }
        .modal-close:hover { opacity: 1; }
        .filter-bar {
            display: flex;
            gap: 12px;
            margin-bottom: 20px;
            flex-wrap: wrap;
            align-items: center;
        }
        .search-input {
            flex: 1;
            min-width: 200px;
            padding: 10px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
        }
        .search-input:focus { outline: none; border-color: var(--accent-color); }
        .group-select {
            padding: 10px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            min-width: 150px;
        }
        .empty-state {
            text-align: center;
            padding: 40px;
            color: rgba(255,255,255,0.5);
        }
        .empty-state i { font-size: 3rem; margin-bottom: 16px; opacity: 0.3; }
        .message {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: none;
        }
        .message.success { background: rgba(139, 92, 246, 0.2); color: #a78bfa; display: block; }
        .message.error { background: rgba(244, 67, 54, 0.2); color: #e57373; display: block; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/" class="back-btn"><i class="fa-solid fa-arrow-left"></i></a>
            <h1>Prompt Library</h1>
            <button class="btn btn-primary" onclick="showCreateModal()">
                <i class="fa-solid fa-plus"></i> New Prompt
            </button>
        </div>

        <div id="message" class="message"></div>

        <div class="filter-bar">
            <input type="text" class="search-input" placeholder="Search prompts..." id="searchInput">
            <select class="group-select" id="groupFilter">
                <option value="">All Groups</option>
            </select>
        </div>

        <div class="prompt-grid" id="promptGrid">
            <div class="empty-state">
                <i class="fa-solid fa-book"></i>
                <p>Loading prompts...</p>
            </div>
        </div>
    </div>

    <div class="modal" id="promptModal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 id="modalTitle">New Prompt</h2>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <form id="promptForm">
                <input type="hidden" id="promptId">
                <div class="form-group">
                    <label>Name *</label>
                    <input type="text" id="promptName" required placeholder="e.g., Code Review Instructions">
                </div>
                <div class="form-group">
                    <label>Group</label>
                    <input type="text" id="promptGroup" value="General" list="groupSuggestions" placeholder="e.g., Code Review, Writing, Analysis">
                    <datalist id="groupSuggestions"></datalist>
                </div>
                <div class="form-group">
                    <label>Description (optional)</label>
                    <input type="text" id="promptDescription" placeholder="Brief description of when to use this prompt">
                </div>
                <div class="form-group">
                    <label>Content * <span id="charCount" style="float: right; font-weight: normal; color: #666;">0 / 50,000</span></label>
                    <textarea id="promptContent" required placeholder="Enter your prompt template..." oninput="updateCharCount()"></textarea>
                </div>
                <div style="display: flex; gap: 12px; justify-content: flex-end;">
                    <button type="button" class="btn btn-danger" id="deleteBtn" onclick="deletePrompt()" style="display: none; margin-right: auto;">Delete</button>
                    <button type="button" class="btn" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        let prompts = [];
        let groups = [];
        let currentPromptId = null;

        async function loadPrompts() {
            const search = document.getElementById('searchInput').value;
            const group = document.getElementById('groupFilter').value;

            let url = '/api/prompts';
            const params = new URLSearchParams();
            if (search) params.set('search', search);
            if (group) params.set('group', group);
            if (params.toString()) url += '?' + params.toString();

            try {
                const response = await fetch(url);
                prompts = await response.json();
                renderPrompts();
            } catch (error) {
                showMessage('Error loading prompts: ' + error.message, 'error');
            }
        }

        async function loadGroups() {
            try {
                const response = await fetch('/api/prompts/groups');
                const data = await response.json();
                groups = data.groups;

                const select = document.getElementById('groupFilter');
                const datalist = document.getElementById('groupSuggestions');

                select.innerHTML = '<option value="">All Groups</option>';
                datalist.innerHTML = '';

                groups.forEach(g => {
                    select.innerHTML += '<option value="' + escapeHtml(g) + '">' + escapeHtml(g) + '</option>';
                    datalist.innerHTML += '<option value="' + escapeHtml(g) + '">';
                });
            } catch (error) {
                console.error('Error loading groups:', error);
            }
        }

        function renderPrompts() {
            const grid = document.getElementById('promptGrid');
            if (prompts.length === 0) {
                grid.innerHTML = '<div class="empty-state"><i class="fa-solid fa-book"></i><p>No prompts found. Create your first prompt!</p></div>';
                return;
            }

            grid.innerHTML = prompts.map(p => `
                <div class="prompt-card" onclick="editPrompt(${p.id})">
                    <div class="prompt-name">${escapeHtml(p.name)}</div>
                    ${p.description ? '<div class="prompt-description">' + escapeHtml(p.description) + '</div>' : ''}
                    <div class="prompt-content">${escapeHtml(p.content)}</div>
                    <div class="prompt-meta">
                        <span class="prompt-group-badge">${escapeHtml(p.group)}</span>
                        <span>Used ${p.usage_count} times</span>
                    </div>
                </div>
            `).join('');
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => msg.className = 'message', 3000);
        }

        function updateCharCount() {
            const content = document.getElementById('promptContent').value;
            const count = content.length;
            const charCount = document.getElementById('charCount');
            charCount.textContent = count.toLocaleString() + ' / 50,000';
            charCount.style.color = count > 50000 ? '#e57373' : (count > 40000 ? '#ffb74d' : '#666');
        }

        function showCreateModal() {
            currentPromptId = null;
            document.getElementById('modalTitle').textContent = 'New Prompt';
            document.getElementById('promptId').value = '';
            document.getElementById('promptName').value = '';
            document.getElementById('promptGroup').value = 'General';
            document.getElementById('promptDescription').value = '';
            document.getElementById('promptContent').value = '';
            document.getElementById('deleteBtn').style.display = 'none';
            document.getElementById('promptModal').classList.add('active');
            updateCharCount();
        }

        function editPrompt(id) {
            const prompt = prompts.find(p => p.id === id);
            if (!prompt) return;

            currentPromptId = id;
            document.getElementById('modalTitle').textContent = 'Edit Prompt';
            document.getElementById('promptId').value = prompt.id;
            document.getElementById('promptName').value = prompt.name;
            document.getElementById('promptGroup').value = prompt.group;
            document.getElementById('promptDescription').value = prompt.description || '';
            document.getElementById('promptContent').value = prompt.content;
            document.getElementById('deleteBtn').style.display = 'block';
            document.getElementById('promptModal').classList.add('active');
            updateCharCount();
        }

        function closeModal() {
            document.getElementById('promptModal').classList.remove('active');
            currentPromptId = null;
        }

        async function deletePrompt() {
            if (!currentPromptId) return;
            if (!confirm('Delete this prompt?')) return;

            try {
                const response = await fetch('/api/prompts/' + currentPromptId, { method: 'DELETE' });
                if (response.ok) {
                    showMessage('Prompt deleted', 'success');
                    closeModal();
                    loadPrompts();
                    loadGroups();
                } else {
                    const error = await response.json();
                    showMessage(error.detail || 'Error deleting prompt', 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        }

        document.getElementById('promptForm').addEventListener('submit', async (e) => {
            e.preventDefault();

            const id = document.getElementById('promptId').value;
            const content = document.getElementById('promptContent').value;

            // Client-side validation for content length
            if (content.length > 50000) {
                showMessage('Content is too long (' + content.length + ' chars). Max: 50,000 characters.', 'error');
                return;
            }

            const data = {
                name: document.getElementById('promptName').value,
                group: document.getElementById('promptGroup').value,
                description: document.getElementById('promptDescription').value,
                content: content
            };

            const url = id ? '/api/prompts/' + id : '/api/prompts';
            const method = id ? 'PATCH' : 'POST';

            try {
                const response = await fetch(url, {
                    method,
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                if (response.ok) {
                    showMessage(id ? 'Prompt updated' : 'Prompt created', 'success');
                    closeModal();
                    loadPrompts();
                    loadGroups();
                } else {
                    // Handle both JSON and non-JSON error responses
                    let errorMsg = 'Error saving prompt';
                    const contentType = response.headers.get('content-type');
                    if (contentType && contentType.includes('application/json')) {
                        const error = await response.json();
                        errorMsg = error.detail || errorMsg;
                    } else {
                        const text = await response.text();
                        if (response.status === 422) {
                            errorMsg = 'Validation error: Check content length (max 50,000 chars) and required fields';
                        } else {
                            errorMsg = 'Server error (' + response.status + '): ' + (text.substring(0, 100) || 'Unknown error');
                        }
                    }
                    showMessage(errorMsg, 'error');
                }
            } catch (error) {
                showMessage('Error: ' + error.message, 'error');
            }
        });

        let searchTimeout;
        document.getElementById('searchInput').addEventListener('input', () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(loadPrompts, 300);
        });

        document.getElementById('groupFilter').addEventListener('change', loadPrompts);

        // Close modal on outside click
        document.getElementById('promptModal').addEventListener('click', (e) => {
            if (e.target.id === 'promptModal') closeModal();
        });

        // Initial load
        loadPrompts();
        loadGroups();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(current_user: User = Depends(get_current_user)):
    """Serve the settings page."""
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot Settings</title>
    <link rel="icon" type="image/png" href="https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/4bmqe5iolvp84ceyk9ttz8vylrym">
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

        <div class='card'><a href='/prompt-library' class='menu-item'><i class='fa-solid fa-book'></i><span>Prompt Library</span><i class='fa-solid fa-chevron-right chevron'></i></a></div>

        <div class='card'>
            <div class='card-header'>Project Context</div>
            <a href='/leonardo-md' class='menu-item'><i class='fa-solid fa-file-lines'></i><span>LEONARDO.md</span><i class='fa-solid fa-chevron-right chevron'></i></a>
        </div>

        {"<div class='card'><div class='card-header'>Automation</div><a href='/scheduled-jobs' class='menu-item'><i class='fa-solid fa-clock'></i><span>Scheduled Jobs</span><i class='fa-solid fa-chevron-right chevron'></i></a></div>" if current_user.role == 'engineer' or current_user.is_admin else ""}

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


@router.post("/logout")
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


@router.get("/leonardo-md", response_class=HTMLResponse)
async def leonardo_md_page(current_user: User = Depends(get_current_user)):
    """Serve the LEONARDO.md editor page."""
    can_edit = current_user.is_admin or getattr(current_user, 'role', 'user') == "engineer"

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot - LEONARDO.md</title>
    <link rel="icon" type="image/png" href="https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/4bmqe5iolvp84ceyk9ttz8vylrym">
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
            max-width: 900px;
            margin: 0 auto;
            padding: 40px 20px;
        }}
        .header {{
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 30px;
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
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .card-header-note {{
            font-size: 0.75rem;
            color: #666;
            text-transform: none;
            letter-spacing: normal;
        }}
        textarea {{
            width: 100%;
            min-height: 400px;
            padding: 16px;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            background: var(--bg-color);
            color: var(--text-color);
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 14px;
            line-height: 1.6;
            resize: vertical;
            box-sizing: border-box;
        }}
        textarea:focus {{
            outline: none;
            border-color: var(--accent-color);
        }}
        textarea:read-only {{
            opacity: 0.7;
            cursor: not-allowed;
        }}
        .btn {{
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-size: 0.95rem;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: background 0.2s;
        }}
        .btn-primary {{
            background: var(--accent-color);
            color: white;
        }}
        .btn-primary:hover {{
            background: #45a049;
        }}
        .btn-primary:disabled {{
            background: #666;
            cursor: not-allowed;
        }}
        .actions {{
            margin-top: 16px;
            display: flex;
            justify-content: flex-end;
            gap: 12px;
        }}
        .message {{
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 16px;
            display: none;
        }}
        .message.success {{
            display: block;
            background: rgba(76, 175, 80, 0.2);
            color: #81c784;
            border: 1px solid rgba(76, 175, 80, 0.3);
        }}
        .message.error {{
            display: block;
            background: rgba(244, 67, 54, 0.2);
            color: #e57373;
            border: 1px solid rgba(244, 67, 54, 0.3);
        }}
        .read-only-notice {{
            color: #888;
            font-size: 0.85rem;
            margin-top: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/settings" class="back-btn">
                <i class="fa-solid fa-arrow-left"></i>
            </a>
            <h1>LEONARDO.md</h1>
        </div>

        <div id="message" class="message"></div>

        <div class="card">
            <div class="card-header">
                Project Context
                <span class="card-header-note">- Appended to all Leonardo agent system prompts</span>
            </div>
            <textarea id="content" placeholder="# Project Context

Add project-specific instructions here...

## About This Project
Describe what this application does and the problem it solves.

## Technical Details
- Framework: Ruby on Rails
- Database: PostgreSQL
- Key gems or dependencies

## Coding Conventions
- Any specific patterns to follow
- Naming conventions
- Testing requirements

## Important Notes
- Business rules to keep in mind
- Edge cases to handle
" {'readonly' if not can_edit else ''}></textarea>
            {'<div class="actions"><button class="btn btn-primary" onclick="saveContent()" id="saveBtn"><i class="fa-solid fa-save"></i> Save</button></div>' if can_edit else '<p class="read-only-notice"><i class="fa-solid fa-lock"></i> View only - engineer or admin role required to edit</p>'}
        </div>
    </div>

    <script>
        async function loadContent() {{
            try {{
                const response = await fetch('/api/leonardo-md');
                const data = await response.json();
                if (data.content !== null) {{
                    document.getElementById('content').value = data.content;
                }}
            }} catch (error) {{
                showMessage('Error loading content: ' + error.message, 'error');
            }}
        }}

        async function saveContent() {{
            const btn = document.getElementById('saveBtn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';

            const content = document.getElementById('content').value;
            try {{
                const response = await fetch('/api/leonardo-md', {{
                    method: 'PUT',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{content: content}})
                }});
                if (response.ok) {{
                    showMessage('Saved successfully!', 'success');
                }} else {{
                    const error = await response.json();
                    showMessage(error.detail || 'Error saving', 'error');
                }}
            }} catch (error) {{
                showMessage('Error: ' + error.message, 'error');
            }} finally {{
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-save"></i> Save';
            }}
        }}

        function showMessage(text, type) {{
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => {{ msg.className = 'message'; }}, 4000);
        }}

        // Load content on page load
        loadContent();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/conversations", response_class=HTMLResponse)
async def conversations(username: str = Depends(auth)):
    with open("conversations.html") as f:
        return f.read()


@router.get("/scheduled-jobs", response_class=HTMLResponse)
async def scheduled_jobs_page(user: User = Depends(engineer_or_admin_required)):
    """Serve the scheduled jobs management page."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>LlamaBot Scheduled Jobs</title>
    <link rel="icon" type="image/png" href="https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/4bmqe5iolvp84ceyk9ttz8vylrym">
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
            max-width: 1100px;
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
        h1 { font-size: 1.5rem; margin: 0; flex: 1; }
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
            display: flex;
            justify-content: space-between;
            align-items: center;
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
        .badge-enabled { background: rgba(76, 175, 80, 0.2); color: #81c784; }
        .badge-disabled { background: rgba(244, 67, 54, 0.2); color: #e57373; }
        .badge-completed { background: rgba(76, 175, 80, 0.2); color: #81c784; }
        .badge-running { background: rgba(33, 150, 243, 0.2); color: #64b5f6; }
        .badge-failed { background: rgba(244, 67, 54, 0.2); color: #e57373; }
        .badge-timeout { background: rgba(255, 152, 0, 0.2); color: #ffb74d; }
        .badge-pending { background: rgba(158, 158, 158, 0.2); color: #bdbdbd; }
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
        .btn-primary {
            background: var(--accent-color);
            border-color: var(--accent-color);
            color: white;
        }
        .btn-primary:hover { opacity: 0.9; background: var(--accent-color); }
        .btn-danger { border-color: #e57373; color: #e57373; }
        .btn-danger:hover { background: rgba(244, 67, 54, 0.2); }
        .btn-sm { padding: 4px 8px; font-size: 11px; }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; margin-bottom: 6px; font-size: 0.85rem; color: #888; }
        .form-group input, .form-group select, .form-group textarea {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            background: var(--bg-color);
            color: var(--text-color);
            box-sizing: border-box;
        }
        .form-group textarea { min-height: 100px; resize: vertical; }
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
            outline: none;
            border-color: var(--accent-color);
        }
        .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        .message {
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: none;
        }
        .message.success { background: rgba(76, 175, 80, 0.2); color: #81c784; display: block; }
        .message.error { background: rgba(244, 67, 54, 0.2); color: #e57373; display: block; }
        .actions { white-space: nowrap; }
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.7);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal.active { display: flex; }
        .modal-content {
            background: var(--chat-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            width: 90%;
            max-width: 600px;
            max-height: 90vh;
            overflow-y: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .modal-header h2 { margin: 0; font-size: 1.2rem; }
        .close-btn {
            background: none;
            border: none;
            color: var(--text-color);
            font-size: 1.5rem;
            cursor: pointer;
        }
        .output-box {
            background: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 12px;
            font-family: monospace;
            font-size: 12px;
            white-space: pre-wrap;
            max-height: 200px;
            overflow-y: auto;
        }
        .tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 20px;
        }
        .tab {
            padding: 8px 16px;
            border: 1px solid var(--border-color);
            border-radius: 6px;
            cursor: pointer;
            background: transparent;
            color: var(--text-color);
        }
        .tab.active {
            background: var(--accent-color);
            border-color: var(--accent-color);
        }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .cron-help {
            font-size: 11px;
            color: #888;
            margin-top: 8px;
        }
        .cron-presets {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-top: 8px;
        }
        .cron-preset {
            padding: 4px 8px;
            background: var(--bg-color);
            border: 1px solid var(--border-color);
            border-radius: 4px;
            font-size: 11px;
            cursor: pointer;
            color: var(--text-color);
            transition: all 0.2s;
        }
        .cron-preset:hover {
            border-color: var(--accent-color);
            background: rgba(76, 175, 80, 0.1);
        }
        .cron-preset code {
            color: #81c784;
            margin-left: 4px;
        }
        .toggle {
            position: relative;
            display: inline-block;
            width: 50px;
            height: 26px;
            cursor: pointer;
        }
        .toggle input { display: none; }
        .toggle-slider {
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #555;
            border-radius: 13px;
            transition: 0.3s;
            border: 2px solid #666;
        }
        .toggle-slider:before {
            content: "";
            position: absolute;
            height: 18px;
            width: 18px;
            left: 2px;
            bottom: 2px;
            background: #aaa;
            border-radius: 50%;
            transition: 0.3s;
            box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }
        .toggle input:checked + .toggle-slider {
            background: var(--accent-color);
            border-color: var(--accent-color);
        }
        .toggle input:checked + .toggle-slider:before {
            transform: translateX(24px);
            background: white;
        }
        .empty-state {
            text-align: center;
            padding: 40px;
            color: #888;
        }
        .empty-state i { font-size: 48px; margin-bottom: 16px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/settings" class="back-btn">
                <i class="fa-solid fa-arrow-left"></i>
            </a>
            <h1>Scheduled Jobs</h1>
            <button class="btn btn-primary" onclick="openCreateModal()">
                <i class="fa-solid fa-plus"></i> New Job
            </button>
        </div>

        <div id="message" class="message"></div>

        <div class="tabs">
            <button class="tab active" onclick="showTab('jobs')">Jobs</button>
            <button class="tab" onclick="showTab('runs')">Recent Runs</button>
            <button class="tab" onclick="showTab('logs')">Logs</button>
        </div>

        <div id="jobs-tab" class="tab-content active">
            <div class="card">
                <div class="card-header">Scheduled Jobs</div>
                <div id="jobsContainer">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Agent</th>
                                <th>Schedule</th>
                                <th>Last Run</th>
                                <th>Next Run</th>
                                <th>Enabled</th>
                                <th class="actions">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="jobsTable">
                            <tr><td colspan="7">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="runs-tab" class="tab-content">
            <div class="card">
                <div class="card-header">Recent Runs</div>
                <table>
                    <thead>
                        <tr>
                            <th>Job</th>
                            <th>Status</th>
                            <th>Trigger</th>
                            <th>Started</th>
                            <th>Duration</th>
                            <th>Tokens</th>
                            <th class="actions">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="runsTable">
                        <tr><td colspan="7">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div id="logs-tab" class="tab-content">
            <div class="card">
                <div class="card-header">Cron Invocation Logs</div>
                <p style="font-size: 12px; color: #888; margin-bottom: 16px;">
                    Every cron ping to /api/scheduled-jobs/invoke is logged here, even when no jobs are due.
                </p>
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Status</th>
                            <th>Jobs Checked</th>
                            <th>Jobs Run</th>
                            <th>Duration</th>
                            <th>Error</th>
                        </tr>
                    </thead>
                    <tbody id="logsTable">
                        <tr><td colspan="6">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Create/Edit Job Modal -->
    <div id="jobModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2 id="modalTitle">Create Job</h2>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <form id="jobForm">
                <input type="hidden" id="jobId">
                <div class="form-group">
                    <label>Name</label>
                    <input type="text" id="jobName" required placeholder="e.g., Daily Code Review">
                </div>
                <div class="form-group">
                    <label>Description (optional)</label>
                    <input type="text" id="jobDescription" placeholder="What this job does">
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Agent</label>
                        <select id="jobAgent" required>
                            <option value="rails_agent">Rails Agent</option>
                            <option value="llamabot">LlamaBot</option>
                            <option value="llamapress">LlamaPress</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Model</label>
                        <select id="jobModel">
                            <option value="gemini-3-flash">Gemini 3 Flash</option>
                            <option value="claude-4.5-haiku">Claude 4.5 Haiku</option>
                            <option value="claude-4.5-sonnet">Claude 4.5 Sonnet</option>
                            <option value="gpt-4o-mini">GPT-4o Mini</option>
                        </select>
                    </div>
                </div>
                <div class="form-group">
                    <label>Prompt / Instructions</label>
                    <textarea id="jobPrompt" required placeholder="What should the agent do when it wakes up?"></textarea>
                </div>
                <div class="form-group">
                    <label>Cron Expression</label>
                    <div class="form-row">
                        <input type="text" id="jobCron" required placeholder="0 8 * * *" style="flex: 2;">
                        <select id="jobTimezone" style="flex: 1;">
                            <option value="UTC">UTC</option>
                            <option value="America/Los_Angeles">Pacific (PT)</option>
                            <option value="America/Denver">Mountain (MT)</option>
                            <option value="America/Chicago">Central (CT)</option>
                            <option value="America/New_York">Eastern (ET)</option>
                            <option value="Africa/Johannesburg">South Africa (SAST)</option>
                        </select>
                    </div>
                    <div class="cron-help">
                        Format: <code>minute hour day-of-month month day-of-week</code>
                    </div>
                    <div class="cron-presets">
                        <button type="button" class="cron-preset" onclick="setCron('* * * * *')">Every minute<code>* * * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('*/5 * * * *')">Every 5 min<code>*/5 * * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('*/30 * * * *')">Every 30 min<code>*/30 * * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 * * * *')">Every hour<code>0 * * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 */4 * * *')">Every 4 hours<code>0 */4 * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 8 * * *')">Daily 8am<code>0 8 * * *</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 9 * * 1')">Weekly Mon 9am<code>0 9 * * 1</code></button>
                        <button type="button" class="cron-preset" onclick="setCron('0 0 1 * *')">Monthly 1st<code>0 0 1 * *</code></button>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Timeout (seconds)</label>
                        <input type="number" id="jobTimeout" value="300" min="60" max="3600">
                    </div>
                    <div class="form-group">
                        <label>Recursion Limit</label>
                        <input type="number" id="jobRecursion" value="100" min="10" max="1000">
                    </div>
                </div>
                <div style="display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px;">
                    <button type="button" class="btn" onclick="closeModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save Job</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Run Details Modal -->
    <div id="runModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Run Details</h2>
                <button class="close-btn" onclick="closeRunModal()">&times;</button>
            </div>
            <div id="runDetails"></div>
        </div>
    </div>

    <script>
        let jobs = [];
        let runs = [];

        async function loadJobs() {
            try {
                const response = await fetch('/api/scheduled-jobs');
                jobs = await response.json();
                renderJobs();
            } catch (e) {
                showMessage('Failed to load jobs: ' + e.message, 'error');
            }
        }

        async function loadRuns() {
            try {
                const response = await fetch('/api/scheduled-jobs/runs/recent?limit=50');
                runs = await response.json();
                renderRuns();
            } catch (e) {
                showMessage('Failed to load runs: ' + e.message, 'error');
            }
        }

        function renderJobs() {
            const tbody = document.getElementById('jobsTable');
            if (jobs.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><i class="fa-solid fa-clock"></i><p>No scheduled jobs yet. Create one to get started!</p></div></td></tr>';
                return;
            }
            tbody.innerHTML = jobs.map(job => `
                <tr>
                    <td><strong>${escapeHtml(job.name)}</strong><br><small style="color:#888">${escapeHtml(job.description || '')}</small></td>
                    <td>${escapeHtml(job.agent_name)}</td>
                    <td><code>${escapeHtml(job.cron_expression)}</code><br><small style="color:#888">${escapeHtml(job.timezone)}</small></td>
                    <td>${job.last_run_at ? formatDate(job.last_run_at) : '-'}</td>
                    <td>${job.next_run_at ? formatDate(job.next_run_at) : '-'}</td>
                    <td>
                        <label class="toggle">
                            <input type="checkbox" ${job.is_enabled ? 'checked' : ''} onchange="toggleJob(${job.id}, this.checked)">
                            <span class="toggle-slider"></span>
                        </label>
                    </td>
                    <td class="actions">
                        <button class="btn btn-sm" onclick="runJob(${job.id})" title="Run Now"><i class="fa-solid fa-play"></i></button>
                        <button class="btn btn-sm" onclick="editJob(${job.id})" title="Edit"><i class="fa-solid fa-pen"></i></button>
                        <button class="btn btn-sm" onclick="viewJobRuns(${job.id})" title="View Runs"><i class="fa-solid fa-history"></i></button>
                    </td>
                </tr>
            `).join('');
        }

        function renderRuns() {
            const tbody = document.getElementById('runsTable');
            if (runs.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><i class="fa-solid fa-history"></i><p>No runs yet</p></div></td></tr>';
                return;
            }
            tbody.innerHTML = runs.map(run => {
                const job = jobs.find(j => j.id === run.job_id);
                return `
                    <tr>
                        <td>${job ? escapeHtml(job.name) : 'Job #' + run.job_id}</td>
                        <td><span class="badge badge-${run.status}">${run.status}</span></td>
                        <td>${run.trigger_type}</td>
                        <td>${run.started_at ? formatTimestamp(run.started_at) : '-'}</td>
                        <td>${run.duration_seconds ? run.duration_seconds.toFixed(1) + 's' : '-'}</td>
                        <td>${run.total_tokens > 0 ? run.total_tokens.toLocaleString() : '-'}</td>
                        <td class="actions">
                            <button class="btn btn-sm" onclick="viewRun(${run.id})" title="View Details"><i class="fa-solid fa-eye"></i></button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        function showTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            const tabIndex = tab === 'jobs' ? 1 : (tab === 'runs' ? 2 : 3);
            document.querySelector(`.tab:nth-child(${tabIndex})`).classList.add('active');
            document.getElementById(tab + '-tab').classList.add('active');
            if (tab === 'runs') loadRuns();
            if (tab === 'logs') loadLogs();
        }

        let invocationLogs = [];

        async function loadLogs() {
            try {
                const response = await fetch('/api/scheduled-jobs/invocations?limit=100');
                invocationLogs = await response.json();
                renderLogs();
            } catch (e) {
                showMessage('Failed to load logs: ' + e.message, 'error');
            }
        }

        function renderLogs() {
            const tbody = document.getElementById('logsTable');
            if (invocationLogs.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state"><i class="fa-solid fa-file-lines"></i><p>No invocation logs yet. The cron will log each ping here.</p></div></td></tr>';
                return;
            }
            tbody.innerHTML = invocationLogs.map(log => `
                <tr>
                    <td>${log.invoked_at ? formatTimestamp(log.invoked_at) : '-'}</td>
                    <td><span class="badge badge-${log.status === 'success' ? 'completed' : (log.status === 'no_jobs_due' ? 'pending' : 'failed')}">${log.status}</span></td>
                    <td>${log.jobs_checked}</td>
                    <td>${log.jobs_executed}</td>
                    <td>${log.duration_ms !== null ? log.duration_ms + 'ms' : '-'}</td>
                    <td style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${log.error_message ? escapeHtml(log.error_message) : ''}">${log.error_message ? escapeHtml(log.error_message.substring(0, 50)) + (log.error_message.length > 50 ? '...' : '') : '-'}</td>
                </tr>
            `).join('');
        }

        function openCreateModal() {
            document.getElementById('modalTitle').textContent = 'Create Job';
            document.getElementById('jobForm').reset();
            document.getElementById('jobId').value = '';
            document.getElementById('jobModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('jobModal').classList.remove('active');
        }

        function closeRunModal() {
            document.getElementById('runModal').classList.remove('active');
        }

        async function editJob(id) {
            const job = jobs.find(j => j.id === id);
            if (!job) return;
            document.getElementById('modalTitle').textContent = 'Edit Job';
            document.getElementById('jobId').value = job.id;
            document.getElementById('jobName').value = job.name;
            document.getElementById('jobDescription').value = job.description || '';
            document.getElementById('jobAgent').value = job.agent_name;
            document.getElementById('jobModel').value = job.llm_model;
            document.getElementById('jobPrompt').value = job.prompt;
            document.getElementById('jobCron').value = job.cron_expression;
            document.getElementById('jobTimezone').value = job.timezone;
            document.getElementById('jobTimeout').value = job.max_duration_seconds;
            document.getElementById('jobRecursion').value = job.recursion_limit;
            document.getElementById('jobModal').classList.add('active');
        }

        async function toggleJob(id, enabled) {
            try {
                await fetch(`/api/scheduled-jobs/${id}/${enabled ? 'enable' : 'disable'}`, { method: 'POST' });
                loadJobs();
            } catch (e) {
                showMessage('Failed to toggle job: ' + e.message, 'error');
            }
        }

        async function runJob(id) {
            if (!confirm('Run this job now?')) return;
            try {
                showMessage('Running job...', 'success');
                const response = await fetch(`/api/scheduled-jobs/${id}/run`, { method: 'POST' });
                const run = await response.json();
                showMessage(`Job completed with status: ${run.status}`, run.status === 'completed' ? 'success' : 'error');
                loadJobs();
                loadRuns();
            } catch (e) {
                showMessage('Failed to run job: ' + e.message, 'error');
            }
        }

        async function viewJobRuns(id) {
            try {
                const response = await fetch(`/api/scheduled-jobs/${id}/runs`);
                runs = await response.json();
                showTab('runs');
                renderRuns();
            } catch (e) {
                showMessage('Failed to load runs: ' + e.message, 'error');
            }
        }

        async function viewRun(id) {
            try {
                const response = await fetch(`/api/scheduled-jobs/runs/${id}`);
                const run = await response.json();
                const job = jobs.find(j => j.id === run.job_id);
                document.getElementById('runDetails').innerHTML = `
                    <p><strong>Job:</strong> ${job ? escapeHtml(job.name) : 'Job #' + run.job_id}</p>
                    <p><strong>Status:</strong> <span class="badge badge-${run.status}">${run.status}</span></p>
                    <p><strong>Trigger:</strong> ${run.trigger_type}</p>
                    <p><strong>Started:</strong> ${run.started_at ? formatTimestamp(run.started_at) : '-'}</p>
                    <p><strong>Duration:</strong> ${run.duration_seconds ? run.duration_seconds.toFixed(2) + ' seconds' : '-'}</p>
                    <p><strong>Tokens:</strong> ${run.input_tokens} in / ${run.output_tokens} out (${run.total_tokens} total)</p>
                    <p><strong>Thread ID:</strong> <code>${run.thread_id}</code></p>
                    ${run.error_type ? `<p><strong>Error Type:</strong> <code style="color:#e57373">${escapeHtml(run.error_type)}</code></p>` : ''}
                    ${run.error_message ? `<p><strong>Error Message:</strong></p><div class="output-box" style="color:#e57373">${escapeHtml(run.error_message)}</div>` : ''}
                    ${run.error_traceback ? `
                        <details style="margin-top: 12px;">
                            <summary style="cursor: pointer; color: #e57373;"><strong>Stack Trace</strong> (click to expand)</summary>
                            <div class="output-box" style="color:#e57373; margin-top: 8px; max-height: 300px;">${escapeHtml(run.error_traceback)}</div>
                        </details>
                    ` : ''}
                    ${run.output_summary ? `<p><strong>Output:</strong></p><div class="output-box">${escapeHtml(run.output_summary)}</div>` : ''}
                `;
                document.getElementById('runModal').classList.add('active');
            } catch (e) {
                showMessage('Failed to load run details: ' + e.message, 'error');
            }
        }

        document.getElementById('jobForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const id = document.getElementById('jobId').value;
            const data = {
                name: document.getElementById('jobName').value,
                description: document.getElementById('jobDescription').value || null,
                agent_name: document.getElementById('jobAgent').value,
                llm_model: document.getElementById('jobModel').value,
                prompt: document.getElementById('jobPrompt').value,
                cron_expression: document.getElementById('jobCron').value,
                timezone: document.getElementById('jobTimezone').value,
                max_duration_seconds: parseInt(document.getElementById('jobTimeout').value),
                recursion_limit: parseInt(document.getElementById('jobRecursion').value),
            };
            try {
                if (id) {
                    await fetch(`/api/scheduled-jobs/${id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    showMessage('Job updated!', 'success');
                } else {
                    await fetch('/api/scheduled-jobs', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(data)
                    });
                    showMessage('Job created!', 'success');
                }
                closeModal();
                loadJobs();
            } catch (e) {
                showMessage('Failed to save job: ' + e.message, 'error');
            }
        });

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            setTimeout(() => { msg.className = 'message'; }, 4000);
        }

        function formatDate(isoString) {
            const d = new Date(isoString);
            const now = new Date();
            const diff = now - d;
            if (diff < 60000) return 'just now';
            if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
            if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
            return d.toLocaleDateString();
        }

        function formatTimestamp(isoString) {
            if (!isoString) return '-';
            const d = new Date(isoString);
            return d.toLocaleString('en-US', {
                month: 'short',
                day: 'numeric',
                year: 'numeric',
                hour: 'numeric',
                minute: '2-digit',
                hour12: true,
                timeZoneName: 'short'
            });
        }

        function escapeHtml(text) {
            if (!text) return '';
            return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        function setCron(expression) {
            document.getElementById('jobCron').value = expression;
        }

        // Load data on page load
        loadJobs();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)
