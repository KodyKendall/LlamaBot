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
