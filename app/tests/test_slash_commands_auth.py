"""
Tests for slash commands role-based authorization.

These tests verify that only users with 'engineer' role or admin privileges
can access slash command endpoints.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from main import app
from app.dependencies import get_current_user, get_db_session
from app.models import User


class TestSlashCommandsAuthorization:
    """Test role-based access control for slash commands."""

    @pytest.fixture
    def mock_db_session(self):
        """Mock database session."""
        mock_session = MagicMock()
        mock_session.add = MagicMock()
        mock_session.commit = MagicMock()
        mock_session.exec = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return mock_session

    @pytest.fixture
    def engineer_user(self):
        """Create a user with engineer role."""
        return User(
            id=1,
            username="engineer_user",
            password_hash="hashed",
            role="engineer",
            is_admin=False,
            is_active=True
        )

    @pytest.fixture
    def admin_user(self):
        """Create an admin user (non-engineer role but is_admin=True)."""
        return User(
            id=2,
            username="admin_user",
            password_hash="hashed",
            role="user",  # Not engineer, but is_admin
            is_admin=True,
            is_active=True
        )

    @pytest.fixture
    def regular_user(self):
        """Create a regular user without engineer role or admin privileges."""
        return User(
            id=3,
            username="regular_user",
            password_hash="hashed",
            role="user",
            is_admin=False,
            is_active=True
        )

    # =========================================================================
    # GET /api/slash-commands - List commands
    # =========================================================================

    def test_get_slash_commands_as_engineer(self, engineer_user):
        """Engineer users can list slash commands."""
        app.dependency_overrides[get_current_user] = lambda: engineer_user

        with TestClient(app) as client:
            response = client.get("/api/slash-commands")

        app.dependency_overrides = {}

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Verify we get the expected commands
        command_names = [cmd["name"] for cmd in data]
        assert "backup" in command_names
        assert "bash" in command_names

    def test_get_slash_commands_as_admin(self, admin_user):
        """Admin users can list slash commands."""
        app.dependency_overrides[get_current_user] = lambda: admin_user

        with TestClient(app) as client:
            response = client.get("/api/slash-commands")

        app.dependency_overrides = {}

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_get_slash_commands_as_regular_user_forbidden(self, regular_user):
        """Regular users (non-engineer, non-admin) cannot list slash commands."""
        app.dependency_overrides[get_current_user] = lambda: regular_user

        with TestClient(app) as client:
            response = client.get("/api/slash-commands")

        app.dependency_overrides = {}

        assert response.status_code == 403
        assert "Engineer or admin privileges required" in response.json()["detail"]

    # =========================================================================
    # POST /api/slash-commands/execute - Execute commands
    # =========================================================================

    @patch("app.routers.slash_commands.execute_host_command")
    @patch("app.routers.slash_commands.log_to_file")
    def test_execute_slash_command_as_engineer(
        self, mock_log, mock_execute, engineer_user, mock_db_session
    ):
        """Engineer users can execute slash commands."""
        mock_execute.return_value = {
            "success": True,
            "stdout": "command output",
            "stderr": "",
            "return_code": 0
        }

        app.dependency_overrides[get_current_user] = lambda: engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        with TestClient(app) as client:
            response = client.post(
                "/api/slash-commands/execute",
                json={"command": "list-backups"}
            )

        app.dependency_overrides = {}

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @patch("app.routers.slash_commands.execute_host_command")
    @patch("app.routers.slash_commands.log_to_file")
    def test_execute_slash_command_as_admin(
        self, mock_log, mock_execute, admin_user, mock_db_session
    ):
        """Admin users can execute slash commands."""
        mock_execute.return_value = {
            "success": True,
            "stdout": "command output",
            "stderr": "",
            "return_code": 0
        }

        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        with TestClient(app) as client:
            response = client.post(
                "/api/slash-commands/execute",
                json={"command": "list-backups"}
            )

        app.dependency_overrides = {}

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_execute_slash_command_as_regular_user_forbidden(
        self, regular_user, mock_db_session
    ):
        """Regular users cannot execute slash commands."""
        app.dependency_overrides[get_current_user] = lambda: regular_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        with TestClient(app) as client:
            response = client.post(
                "/api/slash-commands/execute",
                json={"command": "list-backups"}
            )

        app.dependency_overrides = {}

        assert response.status_code == 403
        assert "Engineer or admin privileges required" in response.json()["detail"]

    # =========================================================================
    # GET /api/slash-commands/history - View command history
    # =========================================================================

    def test_get_command_history_as_engineer(self, engineer_user, mock_db_session):
        """Engineer users can view command history."""
        app.dependency_overrides[get_current_user] = lambda: engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        with TestClient(app) as client:
            response = client.get("/api/slash-commands/history")

        app.dependency_overrides = {}

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_get_command_history_as_admin(self, admin_user, mock_db_session):
        """Admin users can view command history."""
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        with TestClient(app) as client:
            response = client.get("/api/slash-commands/history")

        app.dependency_overrides = {}

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_get_command_history_as_regular_user_forbidden(
        self, regular_user, mock_db_session
    ):
        """Regular users cannot view command history."""
        app.dependency_overrides[get_current_user] = lambda: regular_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        with TestClient(app) as client:
            response = client.get("/api/slash-commands/history")

        app.dependency_overrides = {}

        assert response.status_code == 403
        assert "Engineer or admin privileges required" in response.json()["detail"]

    # =========================================================================
    # Edge cases
    # =========================================================================

    def test_unauthenticated_request_returns_401(self):
        """Requests without authentication return 401."""
        # Don't override dependencies - let the real auth run
        app.dependency_overrides = {}

        with TestClient(app) as client:
            response = client.get("/api/slash-commands")

        assert response.status_code == 401

    def test_engineer_admin_user_can_access(self, mock_db_session):
        """User with both engineer role AND admin flag can access."""
        engineer_admin = User(
            id=4,
            username="super_user",
            password_hash="hashed",
            role="engineer",
            is_admin=True,
            is_active=True
        )

        app.dependency_overrides[get_current_user] = lambda: engineer_admin
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        with TestClient(app) as client:
            response = client.get("/api/slash-commands")

        app.dependency_overrides = {}

        assert response.status_code == 200

    def test_user_with_empty_role_forbidden(self, mock_db_session):
        """User with empty/None role and no admin flag is forbidden."""
        no_role_user = User(
            id=5,
            username="no_role_user",
            password_hash="hashed",
            role="",  # Empty role
            is_admin=False,
            is_active=True
        )

        app.dependency_overrides[get_current_user] = lambda: no_role_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        with TestClient(app) as client:
            response = client.get("/api/slash-commands")

        app.dependency_overrides = {}

        assert response.status_code == 403
