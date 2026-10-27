"""Every checkpoint and git route requires a signed-in user.

These routes read source diffs, run git push/pull/checkout, restart the Rails
app, and discard uncommitted work. Without a session they must answer 401 and
do nothing.
"""
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from main import app
from app.dependencies import get_current_user
from app.models import User
import app.routers.checkpoints as checkpoints


WRITE_ROUTES = [
    ("/api/checkpoints", {"description": "x"}),
    ("/api/checkpoints/abc123/rollback", {"checkpoint_id": "abc123"}),
    ("/api/checkpoints/abc123/accept", None),
    ("/api/checkpoints/discard", None),
    ("/api/checkpoints/discard/undo", None),
    ("/api/checkpoints/discard/dismiss", None),
    ("/api/git/push", None),
    ("/api/git/pull", None),
    ("/api/git/checkout", {"branch": "main"}),
]

READ_ROUTES = [
    "/api/checkpoints",
    "/api/checkpoints/abc123/diff",
    "/api/checkpoints/current-changes",
    "/api/checkpoints/current-head",
    "/api/checkpoints/git-graph",
    "/api/checkpoints/uncommitted",
    "/api/checkpoints/discard/backups",
    "/api/git/remote-status",
]


@pytest.fixture
def anon_client():
    app.dependency_overrides = {}
    yield TestClient(app)
    app.dependency_overrides = {}


def test_every_route_in_the_router_depends_on_get_current_user():
    """Structural guard: a route added later can't forget the dependency."""
    for route in checkpoints.router.routes:
        assert isinstance(route, APIRoute)
        calls = [d.call for d in route.dependant.dependencies]
        assert get_current_user in calls, f"{route.path} has no get_current_user"


@pytest.mark.parametrize("path,body", WRITE_ROUTES)
def test_write_routes_reject_anonymous_and_run_nothing(anon_client, path, body):
    with patch.object(checkpoints, "subprocess") as sub, \
         patch.object(checkpoints, "checkpoint_service") as svc, \
         patch.object(checkpoints, "_restart_llamapress") as restart:
        resp = anon_client.post(path, json=body)
    assert resp.status_code == 401
    assert not sub.mock_calls
    assert not svc.mock_calls
    assert not restart.called


@pytest.mark.parametrize("path", READ_ROUTES)
def test_read_routes_reject_anonymous(anon_client, path):
    with patch.object(checkpoints, "subprocess"), \
         patch.object(checkpoints, "checkpoint_service") as svc:
        resp = anon_client.get(path)
    assert resp.status_code == 401
    assert not svc.mock_calls


def test_signed_in_user_still_gets_through():
    app.dependency_overrides[get_current_user] = lambda: User(id=1, username="owner")
    try:
        with patch.object(checkpoints, "checkpoint_service") as svc:
            svc.get_uncommitted_changes.return_value = {"has_changes": False, "files": []}
            client = TestClient(app)
            assert client.get("/api/checkpoints/uncommitted").status_code == 200
    finally:
        app.dependency_overrides = {}
