"""LP-14: the Scheduled Jobs page shows the Rails app's own recurring jobs.

The schedule lives in the app (db/recurring.yml, run by Solid Queue); LlamaBot
only reads it, through the gem's token-gated ``GET /llama_bot/recurring_tasks``.
These assert plumbing and shape, never a live Rails app.
"""
import asyncio
import hashlib
import hmac

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app
from app.dependencies import engineer_or_admin_required
from app.models import User
from app.services import rails_recurring_tasks
from app.services.rails_recurring_tasks import RailsRecurringTasksClient

SECRET = "test-secret-key-base"
RAILS_PAYLOAD = {
    "available": True,
    "schedule_file": "db/recurring.yml",
    "restart_needed": False,
    "errors": [{"key": "typo", "error": "Class name doesn't correspond to an existing class"}],
    "tasks": [
        {
            "key": "wholesaler_sync",
            "schedule": "every minute",
            "class_name": "WholesalerAutoSyncSchedulerJob",
            "command": None,
            "description": None,
            "next_run_at": "2026-10-27T17:01:00Z",
            "last_runs": [{"run_at": "2026-10-27T17:00:00Z", "status": "finished", "finished_at": "2026-10-27T17:00:02Z", "error": None}],
        }
    ],
}


def _client_with(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return RailsRecurringTasksClient(
        base_url="http://rails.test",
        client_factory=lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
        **kwargs,
    )


class TestRailsRecurringTasksClient:
    def test_sends_the_box_feed_token_and_returns_the_payload(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY_BASE", SECRET)
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=RAILS_PAYLOAD)

        result = asyncio.run(_client_with(handler).fetch())

        expected = hmac.new(SECRET.encode(), b"llamabot-error-feed", hashlib.sha256).hexdigest()
        assert seen["url"] == "http://rails.test/llama_bot/recurring_tasks"
        assert seen["auth"] == f"LlamaBotFeed {expected}"
        assert result["available"] is True
        assert result["tasks"][0]["key"] == "wholesaler_sync"
        assert result["errors"][0]["key"] == "typo"

    def test_rails_without_the_endpoint_reads_as_unavailable_not_an_error(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY_BASE", SECRET)

        result = asyncio.run(_client_with(lambda request: httpx.Response(404)).fetch())

        assert result["available"] is False
        assert result["tasks"] == []
        assert result["reason"]

    def test_rails_down_reads_as_unavailable(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY_BASE", SECRET)

        def handler(request):
            raise httpx.ConnectError("connection refused")

        result = asyncio.run(_client_with(handler).fetch())

        assert result["available"] is False
        assert result["tasks"] == []

    def test_no_shared_secret_means_no_request(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY_BASE", raising=False)
        calls = []

        result = asyncio.run(_client_with(lambda request: calls.append(request) or httpx.Response(200, json=RAILS_PAYLOAD)).fetch())

        assert calls == []
        assert result["available"] is False

    def test_garbled_body_reads_as_unavailable(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY_BASE", SECRET)

        result = asyncio.run(_client_with(lambda request: httpx.Response(200, json=["nope"])).fetch())

        assert result["available"] is False


class TestRailsTasksRoute:
    @pytest.fixture(autouse=True)
    def _clear_overrides(self):
        yield
        app.dependency_overrides = {}

    def test_engineer_gets_the_rails_tasks(self, monkeypatch):
        engineer = User(id=1, username="engineer", role="engineer", is_admin=False, password_hash="x")
        app.dependency_overrides[engineer_or_admin_required] = lambda: engineer

        async def fake_fetch(self):
            return RAILS_PAYLOAD

        monkeypatch.setattr(rails_recurring_tasks.RailsRecurringTasksClient, "fetch", fake_fetch)

        response = TestClient(app).get("/api/scheduled-jobs/rails-tasks")

        assert response.status_code == 200
        body = response.json()
        assert body["tasks"][0]["class_name"] == "WholesalerAutoSyncSchedulerJob"
        assert body["schedule_file"] == "db/recurring.yml"

    def test_signed_out_is_refused(self):
        response = TestClient(app).get("/api/scheduled-jobs/rails-tasks")

        assert response.status_code in (401, 403)

    def test_page_has_the_read_only_app_jobs_section(self):
        from app.routers import ui
        import inspect

        source = inspect.getsource(ui.scheduled_jobs_page)
        assert "/api/scheduled-jobs/rails-tasks" in source
        assert 'id="railsTasks"' in source
