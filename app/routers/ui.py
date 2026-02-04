"""UI/HTML page routes for LlamaBot."""

import logging
from pathlib import Path

from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session

from app.db import engine
from app.models import User
from app.dependencies import (
    security, get_db_session, auth, get_current_user, admin_required, has_any_users
)
from app.services.user_service import authenticate_user, get_user_by_username

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

        # Serve the chat.html file with user role injected
        with open(frontend_dir / "chat.html") as f:
            html = f.read()
        # Inject user role as a global variable for the frontend
        role_script = f'<script>window.LLAMABOT_USER_ROLE = "{getattr(user, "role", "engineer")}";</script>'
        html = html.replace('</head>', f'{role_script}</head>')
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
                    <label>Content *</label>
                    <textarea id="promptContent" required placeholder="Enter your prompt template..."></textarea>
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
            const data = {
                name: document.getElementById('promptName').value,
                group: document.getElementById('promptGroup').value,
                description: document.getElementById('promptDescription').value,
                content: document.getElementById('promptContent').value
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
                    const error = await response.json();
                    showMessage(error.detail || 'Error saving prompt', 'error');
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


@router.get("/conversations", response_class=HTMLResponse)
async def conversations(username: str = Depends(auth)):
    with open("conversations.html") as f:
        return f.read()
