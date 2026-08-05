"""Regression tests: checkpoint restore/discard must not leave root-owned files.

Real incident (leo-palevi-dev, 2026-07-27): a customer clicked **Restore** in the
History panel. The rollback succeeded — and left every touched file owned by root,
because the llamabot container runs git as root while the Leonardo agent runs as uid
1000. The agent then silently failed on the next edit to any restored file.

Same failure class as the mothership memories `root-owned-rails-app-blocks-leo-edits`
and `schema-rb-root-owned-blocks-agent-migrate-dump`. This is the root-run operation
those traced back to. Fleet-wide: every customer who clicks Restore hits it.
"""

import os
import subprocess

import pytest

UBUNTU_UID = 1000
UBUNTU_GID = 1000

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="chown requires root; runs in the llamabot container where uid=0",
)


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def _chown_tree(root, uid=UBUNTU_UID, gid=UBUNTU_GID):
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            try:
                os.lchown(os.path.join(dirpath, name), uid, gid)
            except OSError:
                pass
    os.lchown(root, uid, gid)


def _owners(*paths):
    return [(os.lstat(p).st_uid, os.lstat(p).st_gid) for p in paths]


@pytest.fixture
def leonardo_repo(tmp_path, monkeypatch):
    """A throwaway Leonardo-shaped git repo with two checkpoints."""
    repo = tmp_path / "leonardo"
    (repo / "rails" / "app" / "models").mkdir(parents=True)
    (repo / "bin").mkdir()

    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(repo)],
                   capture_output=True, timeout=5)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    app_file = repo / "rails" / "app" / "models" / "widget.rb"
    app_file.write_text("class Widget; V = 1; end\n")
    (repo / "bin" / "update").write_text("#!/bin/sh\necho v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "checkpoint one")
    first_sha = _git(repo, "rev-parse", "HEAD")

    app_file.write_text("class Widget; V = 2; end\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "checkpoint two")

    # The tree as a real box has it: owned by the agent user, not root.
    _chown_tree(repo)

    from app.services import checkpoint_service

    monkeypatch.setattr(checkpoint_service, "LEONARDO_PATH", repo)
    return {
        "path": repo,
        "first_sha": first_sha,
        "app_file": app_file,
        "service": checkpoint_service.CheckpointService,
    }


def test_rollback_leaves_files_editable_by_the_agent(leonardo_repo):
    """git reset --hard as root rewrites files root-owned; the agent can't edit them."""
    repo = leonardo_repo["path"]
    app_file = leonardo_repo["app_file"]

    assert leonardo_repo["service"].rollback_to_checkpoint(leonardo_repo["first_sha"]) is True
    assert app_file.read_text() == "class Widget; V = 1; end\n", "rollback did not restore"

    uid, gid = _owners(app_file)[0]
    assert (uid, gid) == (UBUNTU_UID, UBUNTU_GID), (
        f"restored file is owned by {uid}:{gid} — the uid-1000 agent cannot edit it"
    )

    # Nothing in the working tree may be root-owned afterwards (excluding .git).
    root_owned = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            if os.lstat(p).st_uid == 0:
                root_owned.append(p)
    assert root_owned == [], f"rollback left root-owned paths: {root_owned}"


def test_discard_uncommitted_changes_leaves_files_editable(leonardo_repo):
    """git checkout -- . as root has the same effect as reset --hard."""
    app_file = leonardo_repo["app_file"]
    app_file.write_text("class Widget; V = 99; end\n")
    os.lchown(app_file, UBUNTU_UID, UBUNTU_GID)

    result = leonardo_repo["service"].discard_uncommitted_changes()
    assert result["success"] is True

    uid, gid = _owners(app_file)[0]
    assert (uid, gid) == (UBUNTU_UID, UBUNTU_GID), (
        f"discarded file is owned by {uid}:{gid} — the uid-1000 agent cannot edit it"
    )


def test_discard_chowns_files_restored_after_untracked_cleanup(leonardo_repo):
    """`git clean -fd` runs too; the surviving tracked files must stay agent-owned."""
    repo = leonardo_repo["path"]
    app_file = leonardo_repo["app_file"]

    app_file.write_text("class Widget; V = 99; end\n")
    stray = repo / "rails" / "app" / "models" / "scratch.rb"
    stray.write_text("# untracked\n")
    _chown_tree(repo)

    result = leonardo_repo["service"].discard_uncommitted_changes()
    assert result["success"] is True
    assert not stray.exists(), "git clean should have removed the untracked file"

    uid, gid = _owners(app_file)[0]
    assert (uid, gid) == (UBUNTU_UID, UBUNTU_GID)


def test_ownership_restore_is_best_effort_and_never_raises(leonardo_repo, monkeypatch):
    """A chown failure must not turn a successful restore into an error."""
    from app.services import checkpoint_service

    def boom(*_args, **_kwargs):
        raise PermissionError("no chown for you")

    monkeypatch.setattr(checkpoint_service.os, "lchown", boom)

    assert leonardo_repo["service"].rollback_to_checkpoint(leonardo_repo["first_sha"]) is True
