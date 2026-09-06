"""The pushed model policy must not wedge Leo's checkpoint commits.

`model_policy_store.save()` writes into `.leonardo/`, which on a customer box is
inside the git working tree. It runs as root in the llamabot container and used
to leave the file root-owned 0600, so `git add -A` — which Leo's automatic
save-point path runs as uid 1000 — aborted with:

    error: unable to index file '.leonardo/model_policy.json'
    fatal: updating files failed

Every box that received a fleet policy after 2026-09-03 silently stopped
checkpointing the customer's work (mothership AgentTask #255). Same failure class
as the root-owned `.git` incident.
"""
import json
import os
import stat

import pytest

from app.services import model_policy_store


@pytest.fixture
def policy_path(tmp_path, monkeypatch):
    target = tmp_path / ".leonardo" / "model_policy.json"
    monkeypatch.setenv(model_policy_store.PATH_ENV, str(target))
    return target


def test_saved_policy_is_world_readable(policy_path):
    """0600 root-owned is what blocked `git add -A`; 0644 does not."""
    model_policy_store.save({"default_model": "deepseek-v4-flash"})

    mode = stat.S_IMODE(policy_path.stat().st_mode)
    assert mode == 0o644, f"expected 0644, got {oct(mode)}"


def test_saved_policy_is_owned_by_the_app_user_when_running_as_root(policy_path, monkeypatch):
    """The llamabot container runs as root; the git tree belongs to uid 1000."""
    chowned = []
    monkeypatch.setattr(model_policy_store.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        model_policy_store.os, "chown",
        lambda p, uid, gid: chowned.append((uid, gid)),
    )

    model_policy_store.save({"default_model": "deepseek-v4-flash"})

    assert chowned == [(1000, 1000)]


def test_save_does_not_chown_when_not_root(policy_path, monkeypatch):
    """On a box where we are already uid 1000 there is nothing to hand over,
    and the call would fail with EPERM."""
    monkeypatch.setattr(model_policy_store.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        model_policy_store.os, "chown",
        lambda *a: pytest.fail("chown attempted as a non-root user"),
    )

    model_policy_store.save({"default_model": "deepseek-v4-flash"})
    assert json.loads(policy_path.read_text())["default_model"] == "deepseek-v4-flash"


def test_save_survives_a_chown_that_is_not_permitted(policy_path, monkeypatch):
    """A policy push must never take the box down over file ownership."""
    def _boom(*a):
        raise PermissionError("nope")

    monkeypatch.setattr(model_policy_store.os, "geteuid", lambda: 0)
    monkeypatch.setattr(model_policy_store.os, "chown", _boom)

    model_policy_store.save({"default_model": "deepseek-v4-flash"})
    assert json.loads(policy_path.read_text())["default_model"] == "deepseek-v4-flash"


def test_save_replaces_a_root_owned_read_only_file(policy_path):
    """Boxes already carrying the bad 0600 file must recover on the next push,
    without a human running chmod."""
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text('{"default_model": "old"}')
    os.chmod(policy_path, 0o400)

    model_policy_store.save({"default_model": "new"})

    assert json.loads(policy_path.read_text())["default_model"] == "new"
    assert stat.S_IMODE(policy_path.stat().st_mode) == 0o644
