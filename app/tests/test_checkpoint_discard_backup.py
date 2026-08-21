"""Regression tests: Discard must never destroy work irrecoverably.

Real incident: a paying customer clicked **Discard** in the History panel and lost
their whole application. `discard_uncommitted_changes` ran `git checkout -- .` and
`git clean -fd` with no snapshot taken first, so every uncommitted file — including
every untracked file, which for a young app is the *entire* app — was gone with no
way back.

The fix: take a stash snapshot BEFORE discarding, pin it under a durable
`refs/llamabot/discards/*` ref so nothing can garbage-collect or `stash clear` it
away, and expose an undo. If the snapshot cannot be taken, the discard must not
happen at all.
"""

import os
import subprocess

import pytest


def _git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15
    )
    if check:
        assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def leonardo_repo(tmp_path, monkeypatch):
    """A throwaway Leonardo-shaped git repo with one commit."""
    repo = tmp_path / "leonardo"
    (repo / "rails" / "app" / "models").mkdir(parents=True)

    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(repo)],
                   capture_output=True, timeout=5)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    app_file = repo / "rails" / "app" / "models" / "widget.rb"
    app_file.write_text("class Widget; V = 1; end\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "committed work")

    from app.services import checkpoint_service

    monkeypatch.setattr(checkpoint_service, "LEONARDO_PATH", repo)
    return {
        "path": repo,
        "app_file": app_file,
        "service": checkpoint_service.CheckpointService,
    }


def _dirty(repo_info):
    """Uncommitted work of both kinds: an edit and a brand-new untracked file."""
    repo_info["app_file"].write_text("class Widget; V = 99; end\n")
    new_file = repo_info["path"] / "rails" / "app" / "models" / "invoice.rb"
    new_file.write_text("class Invoice; end\n")
    return new_file


def test_discard_snapshots_the_work_before_deleting_it(leonardo_repo):
    """The whole point: a discard leaves a recoverable snapshot behind."""
    repo = leonardo_repo["path"]
    new_file = _dirty(leonardo_repo)

    result = leonardo_repo["service"].discard_uncommitted_changes()

    assert result["success"] is True
    assert not new_file.exists(), "untracked file should have been discarded"
    assert leonardo_repo["app_file"].read_text() == "class Widget; V = 1; end\n"

    backup_ref = result.get("backup_ref")
    assert backup_ref, "discard returned no backup ref — the work is unrecoverable"
    sha = _git(repo, "rev-parse", "--verify", backup_ref)
    assert sha == result["backup_sha"]

    # Both kinds of lost work must be inside the snapshot.
    assert "class Widget; V = 99; end" in _git(repo, "show", f"{sha}:rails/app/models/widget.rb")
    assert "class Invoice; end" in _git(repo, "show", f"{sha}^3:rails/app/models/invoice.rb")


def test_undo_restores_everything_the_discard_removed(leonardo_repo):
    """The customer clicks Undo and gets their app back — edits and new files."""
    new_file = _dirty(leonardo_repo)

    leonardo_repo["service"].discard_uncommitted_changes()
    restored = leonardo_repo["service"].restore_discarded_changes()

    assert restored["success"] is True
    assert leonardo_repo["app_file"].read_text() == "class Widget; V = 99; end\n"
    assert new_file.exists() and new_file.read_text() == "class Invoice; end\n"


def test_backup_survives_a_stash_clear(leonardo_repo):
    """The snapshot is pinned by its own ref, not just by the stash stack."""
    repo = leonardo_repo["path"]
    new_file = _dirty(leonardo_repo)

    result = leonardo_repo["service"].discard_uncommitted_changes()
    _git(repo, "stash", "clear")

    restored = leonardo_repo["service"].restore_discarded_changes(result["backup_ref"])
    assert restored["success"] is True
    assert new_file.exists()


def test_discard_refuses_to_run_when_the_snapshot_fails(leonardo_repo, monkeypatch):
    """No backup, no discard. Failing closed is the whole safety property."""
    from app.services import checkpoint_service

    monkeypatch.setattr(
        checkpoint_service, "_snapshot_working_tree",
        lambda *a, **k: (_ for _ in ()).throw(Exception("disk full")),
    )

    new_file = _dirty(leonardo_repo)
    with pytest.raises(Exception) as exc:
        leonardo_repo["service"].discard_uncommitted_changes()

    assert "could not back up" in str(exc.value).lower()
    assert new_file.exists(), "work was destroyed even though the backup failed"
    assert leonardo_repo["app_file"].read_text() == "class Widget; V = 99; end\n"


def test_backups_are_listed_newest_first(leonardo_repo):
    """The undo button needs to know which snapshot is the most recent one."""
    _dirty(leonardo_repo)
    first = leonardo_repo["service"].discard_uncommitted_changes()["backup_ref"]
    _dirty(leonardo_repo)
    second = leonardo_repo["service"].discard_uncommitted_changes()["backup_ref"]

    backups = leonardo_repo["service"].list_discard_backups()
    assert [b["ref"] for b in backups][:2] == [second, first]
    assert backups[0]["file_count"] == 2


def test_undo_reports_clearly_when_there_is_nothing_to_undo(leonardo_repo):
    result = leonardo_repo["service"].restore_discarded_changes()
    assert result["success"] is False
    assert "no" in result["message"].lower()


def test_nothing_to_discard_takes_no_snapshot(leonardo_repo):
    """A clean tree should not litter refs/llamabot/discards with empty snapshots."""
    result = leonardo_repo["service"].discard_uncommitted_changes()
    assert result["discarded_count"] == 0
    assert result.get("backup_ref") is None
    assert leonardo_repo["service"].list_discard_backups() == []


# ---------------- API surface ----------------

def _client():
    from fastapi.testclient import TestClient
    from main import app

    return TestClient(app)


def test_discard_endpoint_hands_the_ui_an_undo_handle(monkeypatch):
    """The UI can only offer Undo if the response carries the backup ref."""
    import app.routers.checkpoints as checkpoints

    monkeypatch.setattr(
        checkpoints.checkpoint_service, "discard_uncommitted_changes",
        staticmethod(lambda: {
            "success": True, "message": "Discarded 3 file(s)", "discarded_count": 3,
            "backup_ref": "refs/llamabot/discards/x", "backup_sha": "deadbeef",
        }),
    )

    resp = _client().post("/api/checkpoints/discard")
    assert resp.status_code == 200
    assert resp.json()["backup_ref"] == "refs/llamabot/discards/x"


def test_undo_endpoint_restores_the_most_recent_discard(monkeypatch):
    import app.routers.checkpoints as checkpoints

    seen = {}

    def fake_restore(backup_ref=None):
        seen["ref"] = backup_ref
        return {"success": True, "message": "Restored 3 file(s)", "restored_files": ["a", "b", "c"]}

    monkeypatch.setattr(
        checkpoints.checkpoint_service, "restore_discarded_changes", staticmethod(fake_restore)
    )
    client = _client()

    assert client.post("/api/checkpoints/discard/undo").json()["success"] is True
    assert seen["ref"] is None, "no ref given should mean 'the latest discard'"

    client.post("/api/checkpoints/discard/undo", json={"backup_ref": "refs/llamabot/discards/x"})
    assert seen["ref"] == "refs/llamabot/discards/x"


def test_backups_endpoint_reports_whether_undo_is_available(monkeypatch):
    import app.routers.checkpoints as checkpoints

    monkeypatch.setattr(
        checkpoints.checkpoint_service, "list_discard_backups",
        staticmethod(lambda *a, **k: []),
    )
    assert _client().get("/api/checkpoints/discard/backups").json()["has_backups"] is False
