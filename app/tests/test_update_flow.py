"""Self-update flow: POST /api/update must report failures honestly, remember
them, and tell the mothership; GET /api/check-updates must stop offering a
version pair that already failed here (the banner otherwise nags forever and
every click costs the user a 3-minute dead wait).

Run with: pytest app/tests/test_update_flow.py -v
"""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.dependencies import get_current_user, get_db_session
from app.models import SiteSetting, User


ENGINEER = User(id=1, username="engineer", password_hash="x", role="engineer",
                is_admin=False, is_active=True)
ADMIN = User(id=2, username="admin", password_hash="x", role="user",
             is_admin=True, is_active=True)
REGULAR = User(id=3, username="viewer", password_hash="x", role="user",
               is_admin=False, is_active=True)

PAIR = {"llamabot_version": "0.5.0", "llamapress_version": "1.2.0"}
MARKER_KEY = "update_failed:0.5.0:1.2.0"

UPDATE_OFFER = {
    "updates_available": True,
    "latest_versions": {
        "llamabot": {"version": "0.5.0", "notes": ["stuff"]},
        "llamapress": {"version": "1.2.0", "notes": []},
    },
}


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def engine():
    # StaticPool + check_same_thread: TestClient runs the app on another thread,
    # and a default in-memory SQLite engine would hand it a fresh, empty DB.
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def client(engine):
    from app.routers.api import router

    app = FastAPI()
    app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_current_user] = lambda: ENGINEER

    c = TestClient(app)
    c._app = app
    c._engine = engine
    return c


@pytest.fixture
def mothership():
    instance = MagicMock()
    instance.enabled = True
    instance.check_updates = AsyncMock(return_value=UPDATE_OFFER)
    instance.report_error = AsyncMock(return_value=None)
    # Both endpoints lazy-import MothershipClient inside the function body, so
    # patching the source module attribute intercepts them.
    with patch("app.services.mothership_client.MothershipClient", return_value=instance):
        yield instance


@pytest.fixture
def versions():
    with patch("app.routers.api.get_container_version", return_value="0.4.9"), \
         patch("app.routers.api.get_llamapress_version", return_value="1.1.0"):
        yield


def _get_marker(engine, key=MARKER_KEY):
    with Session(engine) as s:
        return s.get(SiteSetting, key)


# =========================================================================
# POST /api/update — honest failure reporting
# =========================================================================

class TestPerformUpdateHonesty:

    def test_update_timeout_is_reported_as_failure(self, client, mothership, versions):
        """The headline bug: a 300s subprocess timeout used to return success:true."""
        with patch("app.routers.slash_commands.execute_command",
                   side_effect=subprocess.TimeoutExpired(cmd="bin/update", timeout=300)):
            r = client.post("/api/update", json=PAIR)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body.get("error")

    def test_update_oserror_is_reported_as_failure(self, client, mothership, versions):
        with patch("app.routers.slash_commands.execute_command",
                   side_effect=FileNotFoundError("nsenter: not found")):
            r = client.post("/api/update", json=PAIR)
        body = r.json()
        assert body["success"] is False
        assert "nsenter" in body.get("error", "")

    def test_update_nonzero_exit_includes_error_tail(self, client, mothership, versions):
        with patch("app.routers.slash_commands.execute_command",
                   return_value=_completed(1, stderr="fatal: could not pull image")):
            r = client.post("/api/update", json=PAIR)
        body = r.json()
        assert body["success"] is False
        assert "could not pull" in body.get("error", "")

    def test_update_failure_persists_marker(self, client, mothership, versions):
        with patch("app.routers.slash_commands.execute_command",
                   return_value=_completed(1, stderr="fatal: could not pull image")):
            client.post("/api/update", json=PAIR)
        row = _get_marker(client._engine)
        assert row is not None
        value = json.loads(row.value)
        assert "could not pull" in value["error"]
        assert value["at"]

    def test_update_failure_reports_to_mothership(self, client, mothership, versions):
        with patch("app.routers.slash_commands.execute_command",
                   return_value=_completed(1, stderr="boom")):
            client.post("/api/update", json=PAIR)
        assert mothership.report_error.await_count == 1
        kwargs = mothership.report_error.await_args.kwargs
        assert kwargs["fingerprint"] == MARKER_KEY
        assert kwargs["recovered"] is False
        assert kwargs["error_class"]
        assert kwargs["error_message"]

    def test_update_success_writes_no_marker_no_telemetry(self, client, mothership, versions):
        with patch("app.routers.slash_commands.execute_command",
                   return_value=_completed(0, stdout="Update initiated")):
            r = client.post("/api/update", json=PAIR)
        assert r.json()["success"] is True
        assert _get_marker(client._engine) is None
        mothership.report_error.assert_not_awaited()

    def test_update_marker_write_is_best_effort(self, client, mothership, versions):
        """A down auth DB must not turn an honest failure report into a 500."""
        bad_session = MagicMock()
        bad_session.get.side_effect = RuntimeError("db down")
        client._app.dependency_overrides[get_db_session] = lambda: bad_session
        with patch("app.routers.slash_commands.execute_command",
                   return_value=_completed(1, stderr="boom")):
            r = client.post("/api/update", json=PAIR)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body.get("error")

    def test_update_forbidden_for_user_role(self, client, mothership, versions):
        client._app.dependency_overrides[get_current_user] = lambda: REGULAR
        with patch("app.routers.slash_commands.execute_command") as mock_exec:
            r = client.post("/api/update", json=PAIR)
        assert r.status_code == 403
        mock_exec.assert_not_called()

    def test_update_failure_key_too_long_skips_marker(self, client, mothership, versions):
        long_pair = {"llamabot_version": "a" * 60, "llamapress_version": "b" * 60}
        with patch("app.routers.slash_commands.execute_command",
                   return_value=_completed(1, stderr="boom")):
            r = client.post("/api/update", json=long_pair)
        assert r.json()["success"] is False
        with Session(client._engine) as s:
            from sqlmodel import select
            rows = s.exec(select(SiteSetting)).all()
        assert rows == []


# =========================================================================
# GET /api/check-updates — suppression after a failed attempt
# =========================================================================

class TestCheckUpdatesSuppression:

    def _seed_marker(self, engine, key=MARKER_KEY):
        with Session(engine) as s:
            s.add(SiteSetting(key=key, value=json.dumps({"at": "2026-07-20T00:00:00Z",
                                                         "error": "boom"})))
            s.commit()

    def test_check_updates_suppressed_after_failed_pair(self, client, mothership, versions):
        self._seed_marker(client._engine)
        r = client.get("/api/check-updates")
        assert r.status_code == 200
        assert r.json() == {"updates_available": False,
                            "reason": "previous_update_failed"}

    def test_check_updates_new_release_unsticks(self, client, mothership, versions):
        """The marker is per version pair — the NEXT release must re-offer."""
        self._seed_marker(client._engine)
        newer = {
            "updates_available": True,
            "latest_versions": {
                "llamabot": {"version": "0.5.1", "notes": []},
                "llamapress": {"version": "1.2.0", "notes": []},
            },
        }
        mothership.check_updates = AsyncMock(return_value=newer)
        r = client.get("/api/check-updates")
        assert r.json()["updates_available"] is True
        assert r.json()["latest_versions"]["llamabot"]["version"] == "0.5.1"

    def test_check_updates_no_marker_passes_through(self, client, mothership, versions):
        r = client.get("/api/check-updates")
        assert r.json() == UPDATE_OFFER

    def test_check_updates_not_configured_still_works(self, client, mothership, versions):
        mothership.enabled = False
        r = client.get("/api/check-updates")
        assert r.json() == {"updates_available": False,
                            "reason": "mothership_not_configured"}

    def test_check_updates_check_failed_still_works(self, client, mothership, versions):
        mothership.check_updates = AsyncMock(return_value=None)
        r = client.get("/api/check-updates")
        assert r.json() == {"updates_available": False, "reason": "check_failed"}

    def test_check_updates_db_down_fails_open(self, client, mothership, versions):
        """Suppression must never hide a real update because the DB hiccupped."""
        bad_session = MagicMock()
        bad_session.get.side_effect = RuntimeError("db down")
        client._app.dependency_overrides[get_db_session] = lambda: bad_session
        r = client.get("/api/check-updates")
        assert r.json()["updates_available"] is True

    def test_update_failure_then_check_updates_roundtrip(self, client, mothership, versions):
        """Writer and reader must agree on the key format — end to end."""
        with patch("app.routers.slash_commands.execute_command",
                   return_value=_completed(1, stderr="fatal: could not pull image")):
            client.post("/api/update", json=PAIR)
        r = client.get("/api/check-updates")
        assert r.json() == {"updates_available": False,
                            "reason": "previous_update_failed"}
