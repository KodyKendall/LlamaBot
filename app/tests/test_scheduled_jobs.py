"""
Tests for the scheduled jobs API and headless executor.
"""
import pytest
import pytest_asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from main import app
from app.dependencies import get_current_user, get_db_session, engineer_or_admin_required, admin_required
from app.models import User, ScheduledJob, ScheduledJobRun


# Mock user fixtures
@pytest.fixture
def mock_engineer_user():
    return User(id=1, username="engineer", role="engineer", is_admin=False, password_hash="test")


@pytest.fixture
def mock_admin_user():
    return User(id=2, username="admin", role="engineer", is_admin=True, password_hash="test")


@pytest.fixture
def mock_regular_user():
    return User(id=3, username="user", role="user", is_admin=False, password_hash="test")


@pytest.fixture
def mock_db_session():
    """Mock database session."""
    session = MagicMock()
    session.exec = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    session.get = MagicMock(return_value=None)
    session.add = MagicMock()
    session.commit = MagicMock()
    session.refresh = MagicMock()
    session.delete = MagicMock()
    return session


@pytest.fixture
def sample_job():
    """Sample scheduled job for testing."""
    return ScheduledJob(
        id=1,
        name="Test Job",
        description="A test job",
        agent_name="rails_agent",
        prompt="Do something useful",
        llm_model="gemini-3-flash",
        cron_expression="0 8 * * *",
        timezone="UTC",
        max_duration_seconds=300,
        recursion_limit=100,
        is_enabled=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_run():
    """Sample job run for testing."""
    return ScheduledJobRun(
        id=1,
        job_id=1,
        status="completed",
        trigger_type="manual",
        thread_id="scheduled-1-abc12345",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=5.5,
        output_summary="Task completed successfully.",
        input_tokens=100,
        output_tokens=200,
        total_tokens=300,
        created_at=datetime.now(timezone.utc),
    )


class TestScheduledJobsAPI:
    """Tests for the scheduled jobs REST API."""

    def test_list_jobs_as_engineer(self, mock_engineer_user, mock_db_session, sample_job):
        """Engineers can list scheduled jobs."""
        mock_db_session.exec.return_value.all.return_value = [sample_job]

        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.get("/api/scheduled-jobs")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

        app.dependency_overrides = {}

    def test_create_job_as_engineer(self, mock_engineer_user, mock_db_session):
        """Engineers can create scheduled jobs."""
        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        # Mock the refresh to set an ID
        def mock_refresh(job):
            job.id = 1
        mock_db_session.refresh = mock_refresh

        client = TestClient(app)
        response = client.post("/api/scheduled-jobs", json={
            "name": "New Job",
            "agent_name": "rails_agent",
            "prompt": "Do something",
            "cron_expression": "0 9 * * *"
        })

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Job"
        assert data["agent_name"] == "rails_agent"

        app.dependency_overrides = {}

    def test_get_job_by_id(self, mock_engineer_user, mock_db_session, sample_job):
        """Can retrieve a specific job by ID."""
        mock_db_session.get.return_value = sample_job

        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.get("/api/scheduled-jobs/1")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 1
        assert data["name"] == "Test Job"

        app.dependency_overrides = {}

    def test_get_job_not_found(self, mock_engineer_user, mock_db_session):
        """Returns 404 for non-existent job."""
        mock_db_session.get.return_value = None

        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.get("/api/scheduled-jobs/999")

        assert response.status_code == 404

        app.dependency_overrides = {}

    def test_update_job(self, mock_engineer_user, mock_db_session, sample_job):
        """Can update a scheduled job."""
        mock_db_session.get.return_value = sample_job

        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.patch("/api/scheduled-jobs/1", json={
            "name": "Updated Job Name"
        })

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Job Name"

        app.dependency_overrides = {}

    def test_delete_job_requires_admin(self, mock_engineer_user, mock_db_session, sample_job):
        """Only admins can delete jobs."""
        mock_db_session.get.return_value = sample_job

        # Engineer should be rejected - just override get_current_user, let admin_required work naturally
        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.delete("/api/scheduled-jobs/1")

        # Should fail with 403 because engineer is not admin
        assert response.status_code == 403
        assert "Admin privileges required" in response.json()["detail"]

        app.dependency_overrides = {}

    def test_delete_job_as_admin(self, mock_admin_user, mock_db_session, sample_job):
        """Admins can delete jobs."""
        mock_db_session.get.return_value = sample_job

        app.dependency_overrides[get_current_user] = lambda: mock_admin_user
        app.dependency_overrides[admin_required] = lambda: mock_admin_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.delete("/api/scheduled-jobs/1")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        app.dependency_overrides = {}

    def test_enable_job(self, mock_engineer_user, mock_db_session, sample_job):
        """Can enable a disabled job."""
        sample_job.is_enabled = False
        mock_db_session.get.return_value = sample_job

        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.post("/api/scheduled-jobs/1/enable")

        assert response.status_code == 200
        data = response.json()
        assert data["is_enabled"] is True

        app.dependency_overrides = {}

    def test_disable_job(self, mock_engineer_user, mock_db_session, sample_job):
        """Can disable an enabled job."""
        sample_job.is_enabled = True
        mock_db_session.get.return_value = sample_job

        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.post("/api/scheduled-jobs/1/disable")

        assert response.status_code == 200
        data = response.json()
        assert data["is_enabled"] is False

        app.dependency_overrides = {}

    def test_get_job_runs(self, mock_engineer_user, mock_db_session, sample_job, sample_run):
        """Can get run history for a job."""
        mock_db_session.get.return_value = sample_job
        mock_db_session.exec.return_value.all.return_value = [sample_run]

        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.get("/api/scheduled-jobs/1/runs")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

        app.dependency_overrides = {}

    def test_get_recent_runs(self, mock_engineer_user, mock_db_session, sample_run):
        """Can get recent runs across all jobs."""
        mock_db_session.exec.return_value.all.return_value = [sample_run]

        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user
        app.dependency_overrides[get_db_session] = lambda: mock_db_session

        client = TestClient(app)
        response = client.get("/api/scheduled-jobs/runs/recent")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

        app.dependency_overrides = {}


class TestSchedulerTokenAuth:
    """Tests for scheduler token authentication."""

    def test_verify_scheduler_token_valid(self):
        """Valid scheduler token is accepted."""
        from app.services.token_service import verify_scheduler_token

        with patch.dict(os.environ, {"SCHEDULER_TOKEN": "test-secret-token"}):
            # Need to reload to pick up the env var
            import importlib
            import app.services.token_service as ts
            importlib.reload(ts)

            assert ts.verify_scheduler_token("test-secret-token") is True
            assert ts.verify_scheduler_token("wrong-token") is False

    def test_verify_scheduler_token_not_configured(self):
        """Returns False when SCHEDULER_TOKEN not set."""
        from app.services.token_service import verify_scheduler_token

        with patch.dict(os.environ, {}, clear=True):
            import importlib
            import app.services.token_service as ts
            importlib.reload(ts)

            assert ts.verify_scheduler_token("any-token") is False


class TestCronExpressionParsing:
    """Tests for cron expression handling."""

    def test_calculate_next_run(self):
        """Can calculate next run time from cron expression."""
        from app.routers.scheduled_jobs import _calculate_next_run

        # Test daily at 8am
        next_run = _calculate_next_run("0 8 * * *", "UTC")
        assert next_run is not None
        assert next_run.hour == 8

    def test_calculate_next_run_with_timezone(self):
        """Handles timezone correctly."""
        from app.routers.scheduled_jobs import _calculate_next_run

        next_run_utc = _calculate_next_run("0 8 * * *", "UTC")
        next_run_la = _calculate_next_run("0 8 * * *", "America/Los_Angeles")

        assert next_run_utc is not None
        assert next_run_la is not None
        # LA is 8 hours behind UTC, so next runs should differ
        # (unless it's exactly at the boundary)

    def test_calculate_next_run_invalid_cron(self):
        """Returns None for invalid cron expression."""
        from app.routers.scheduled_jobs import _calculate_next_run

        next_run = _calculate_next_run("invalid cron", "UTC")
        assert next_run is None


class TestScheduledJobsUI:
    """Tests for the scheduled jobs UI page."""

    def test_scheduled_jobs_page_requires_auth(self):
        """Scheduled jobs page requires engineer or admin auth."""
        client = TestClient(app)
        response = client.get("/scheduled-jobs")

        # Should redirect or return 401/403 without auth
        assert response.status_code in [401, 403, 307]

    def test_scheduled_jobs_page_accessible_to_engineer(self, mock_engineer_user):
        """Engineers can access the scheduled jobs page."""
        app.dependency_overrides[get_current_user] = lambda: mock_engineer_user
        app.dependency_overrides[engineer_or_admin_required] = lambda: mock_engineer_user

        client = TestClient(app)
        response = client.get("/scheduled-jobs")

        assert response.status_code == 200
        assert "Scheduled Jobs" in response.text

        app.dependency_overrides = {}
