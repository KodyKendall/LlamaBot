"""Tests for the git pull / update endpoints (the 'pull updates' button).

These assert the structure/behaviour of the endpoints (response shape, that a
fast-forward refusal yields the friendly message, that a successful pull triggers
a restart, that checkout validates the branch). git/docker subprocess calls are
mocked — we never touch a real repo or container.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app
import app.routers.checkpoints as checkpoints


def _completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


client = TestClient(app)


# ---------------- /api/git/remote-status ----------------

def test_remote_status_reports_behind_and_branches():
    def fake_git(*args, timeout=60):
        a = args
        if a[:1] == ("fetch",):
            return _completed(0)
        if a[:2] == ("rev-parse", "--abbrev-ref") and a[-1] == "HEAD":
            return _completed(0, stdout="main\n")
        if a[:1] == ("rev-parse",) and "@{u}" in a:
            return _completed(0, stdout="origin/main\n")
        if a[:1] == ("rev-list",):
            return _completed(0, stdout="0\t2\n")  # ahead=0, behind=2
        if a[:2] == ("branch", "-r"):
            return _completed(0, stdout="origin/HEAD -> origin/main\norigin/main\norigin/dev\n")
        return _completed(0)

    with patch.object(checkpoints, "_git", side_effect=fake_git):
        resp = client.get("/api/git/remote-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["branch"] == "main"
    assert data["behind"] == 2
    assert data["ahead"] == 0
    assert data["up_to_date"] is False
    # origin/ prefix stripped, HEAD pointer excluded
    assert data["branches"] == ["main", "dev"]


def test_remote_status_up_to_date():
    def fake_git(*args, timeout=60):
        if args[:1] == ("fetch",):
            return _completed(0)
        if args[:2] == ("rev-parse", "--abbrev-ref") and args[-1] == "HEAD":
            return _completed(0, stdout="main\n")
        if args[:1] == ("rev-parse",) and "@{u}" in args:
            return _completed(0, stdout="origin/main\n")
        if args[:1] == ("rev-list",):
            return _completed(0, stdout="0\t0\n")
        if args[:2] == ("branch", "-r"):
            return _completed(0, stdout="origin/main\n")
        return _completed(0)

    with patch.object(checkpoints, "_git", side_effect=fake_git):
        data = client.get("/api/git/remote-status").json()

    assert data["behind"] == 0
    assert data["up_to_date"] is True


def test_remote_status_fetch_failure_is_reported():
    with patch.object(checkpoints, "_git", return_value=_completed(1, stderr="network down")):
        data = client.get("/api/git/remote-status").json()
    assert data["success"] is False
    assert "network down" in data["message"]


# ---------------- /api/git/pull ----------------

def test_pull_success_triggers_restart():
    with patch.object(checkpoints, "_git", return_value=_completed(0, stdout="Updating…")) as g, \
         patch.object(checkpoints, "_restart_llamapress", return_value=(True, "restarting")) as r:
        resp = client.post("/api/git/pull")

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["restarted"] is True
    # pull used --ff-only
    assert g.call_args.args[:2] == ("pull", "--ff-only")
    r.assert_called_once()


def test_pull_ff_blocked_returns_friendly_message_and_no_restart():
    blocked = _completed(1, stderr="Your local changes would be overwritten")
    with patch.object(checkpoints, "_git", return_value=blocked), \
         patch.object(checkpoints, "_restart_llamapress") as r:
        data = client.post("/api/git/pull").json()

    assert data["success"] is False
    assert data["message"] == checkpoints.FF_BLOCKED_MESSAGE
    assert "commit or discard" in data["message"].lower()
    r.assert_not_called()  # never restart when the pull didn't apply


# ---------------- /api/git/checkout ----------------

def test_checkout_rejects_unknown_branch():
    with patch.object(checkpoints, "_remote_branches", return_value=["main", "dev"]):
        resp = client.post("/api/git/checkout", json={"branch": "not-a-branch"})
    assert resp.status_code == 400


def test_checkout_rejects_flag_like_branch():
    # A value that could be mistaken for a git flag must be rejected by validation.
    with patch.object(checkpoints, "_remote_branches", return_value=["main"]):
        resp = client.post("/api/git/checkout", json={"branch": "--hard"})
    assert resp.status_code == 400


def test_checkout_success_switches_pulls_and_restarts():
    with patch.object(checkpoints, "_remote_branches", return_value=["main", "dev"]), \
         patch.object(checkpoints, "_git", return_value=_completed(0)) as g, \
         patch.object(checkpoints, "_restart_llamapress", return_value=(True, "restarting")) as r:
        data = client.post("/api/git/checkout", json={"branch": "dev"}).json()

    assert data["success"] is True
    assert data["branch"] == "dev"
    # both checkout and a follow-up ff-only pull happened
    called = [c.args for c in g.call_args_list]
    assert ("checkout", "dev") in called
    assert ("pull", "--ff-only") in called
    r.assert_called_once()
