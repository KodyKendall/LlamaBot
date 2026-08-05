"""Regression tests: a checkpoint restore must not revert platform files.

Real incident (leo-palevi-dev, 2026-07-27): a customer clicked **Restore** to get his
own application code back. A checkpoint commits the WHOLE ~/Leonardo tree, which holds
the customer's app AND the platform files `bin/update` installs — so the rollback also
reverted 21 platform files to their pre-update state. The containers kept running the
NEW images. Result: new images, old platform files.

Two confirmed consequences on that box:
  1. The restored docker-compose.yml + langgraph.json dropped
     `rails_engineer_plan_mode_agent`. The next `docker compose up -d` would break
     Engineer Mode — a direct repeat of SupportIncident #179 on the same box, three
     days after we fixed it.
  2. Two applied migrations lost their files (`up 20260720000000 ***** NO FILE *****`).
     The database had them; the tree did not.

The fix re-applies the platform ALLOWLIST from the pre-rollback HEAD. The allowlist is
NOT invented here — it is the same one `bin/update` syncs (Leonardo bin/update:76).
"""

import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0,
    reason="runs in the llamabot container (git writes as root, like production)",
)


def _git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15
    )
    if check:
        assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def leonardo_repo(tmp_path, monkeypatch):
    """A Leonardo-shaped repo: app code + platform files, updated after a checkpoint."""
    repo = tmp_path / "leonardo"
    (repo / "rails" / "app" / "models").mkdir(parents=True)
    (repo / "rails" / "db" / "migrate").mkdir(parents=True)
    (repo / "rails" / "app" / "javascript" / "llamapress").mkdir(parents=True)
    (repo / "langgraph").mkdir()
    (repo / "bin").mkdir()

    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(repo)],
                   capture_output=True, timeout=5)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    app_file = repo / "rails" / "app" / "models" / "widget.rb"
    app_file.write_text("class Widget; V = 1; end\n")
    (repo / "bin" / "update").write_text("#!/bin/sh\n# platform v1\n")
    (repo / "docker-compose.yml").write_text("services:\n  llamabot:\n    image: llamabot:0.6.0d\n")
    (repo / "langgraph" / "langgraph.json").write_text('{"graphs": {"rails_agent": "x"}}\n')
    (repo / "rails" / "app" / "javascript" / "llamapress" / "bubble.js").write_text("// v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "customer checkpoint (pre-update)")
    checkpoint_sha = _git(repo, "rev-parse", "HEAD")

    # A platform update lands (bin/update): new platform files, and the customer keeps
    # working on his app.
    (repo / "bin" / "update").write_text("#!/bin/sh\n# platform v2\n")
    (repo / "docker-compose.yml").write_text("services:\n  llamabot:\n    image: llamabot:0.6.0e\n")
    (repo / "langgraph" / "langgraph.json").write_text(
        '{"graphs": {"rails_agent": "x", "rails_engineer_plan_mode_agent": "y"}}\n'
    )
    (repo / "rails" / "app" / "javascript" / "llamapress" / "bubble.js").write_text("// v2\n")
    (repo / "rails" / "db" / "migrate" / "20260720000000_add_trackable_to_users.rb").write_text(
        "class AddTrackableToUsers < ActiveRecord::Migration[8.0]; end\n"
    )
    app_file.write_text("class Widget; V = 2; end\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "platform update + customer work")

    from app.services import checkpoint_service

    monkeypatch.setattr(checkpoint_service, "LEONARDO_PATH", repo)
    return {
        "path": repo,
        "checkpoint_sha": checkpoint_sha,
        "app_file": app_file,
        "service": checkpoint_service.CheckpointService,
    }


def test_customer_app_code_is_restored(leonardo_repo):
    """The thing the customer actually asked for must still work."""
    assert leonardo_repo["service"].rollback_to_checkpoint(leonardo_repo["checkpoint_sha"]) is True
    assert leonardo_repo["app_file"].read_text() == "class Widget; V = 1; end\n"


def test_platform_files_survive_the_rollback(leonardo_repo):
    repo = leonardo_repo["path"]

    leonardo_repo["service"].rollback_to_checkpoint(leonardo_repo["checkpoint_sha"])

    assert (repo / "bin" / "update").read_text() == "#!/bin/sh\n# platform v2\n", \
        "bin/update was reverted — the box now runs new images with an old updater"
    assert "0.6.0e" in (repo / "docker-compose.yml").read_text(), \
        "docker-compose.yml was reverted — the next `up -d` re-applies old image tags"
    assert (repo / "rails" / "app" / "javascript" / "llamapress" / "bubble.js").read_text() == "// v2\n"


def test_engineer_mode_graph_is_not_dropped(leonardo_repo):
    """SI#179's exact regression: langgraph.json losing rails_engineer_plan_mode_agent."""
    repo = leonardo_repo["path"]

    leonardo_repo["service"].rollback_to_checkpoint(leonardo_repo["checkpoint_sha"])

    graphs = (repo / "langgraph" / "langgraph.json").read_text()
    assert "rails_engineer_plan_mode_agent" in graphs, \
        "restoring dropped the Engineer Mode graph — the next `up -d` breaks Engineer Mode"


def test_applied_migrations_keep_their_files(leonardo_repo):
    """A migration row in the DB with no file on disk is unrecoverable by the agent."""
    repo = leonardo_repo["path"]

    leonardo_repo["service"].rollback_to_checkpoint(leonardo_repo["checkpoint_sha"])

    migration = repo / "rails" / "db" / "migrate" / "20260720000000_add_trackable_to_users.rb"
    assert migration.exists(), "the applied migration lost its file (***** NO FILE *****)"


def test_platform_reapply_is_committed_as_one_readable_commit(leonardo_repo):
    """The customer's History must stay readable, not sprout a dirty tree."""
    repo = leonardo_repo["path"]

    leonardo_repo["service"].rollback_to_checkpoint(leonardo_repo["checkpoint_sha"])

    status = _git(repo, "status", "--porcelain")
    assert status == "", f"rollback left the tree dirty: {status}"

    subject = _git(repo, "log", "-1", "--format=%s")
    assert "platform" in subject.lower(), f"unhelpful commit subject: {subject!r}"


def test_no_platform_commit_when_nothing_platform_changed(leonardo_repo):
    """Rolling back a checkpoint taken AFTER the update must not add a noise commit."""
    repo = leonardo_repo["path"]

    # A second customer-only checkpoint, after the platform update.
    leonardo_repo["app_file"].write_text("class Widget; V = 3; end\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more customer work")
    recent_sha = _git(repo, "rev-parse", "HEAD~1")
    before = _git(repo, "rev-list", "--count", "HEAD")

    leonardo_repo["service"].rollback_to_checkpoint(recent_sha)

    after = _git(repo, "rev-list", "--count", "HEAD")
    assert after == before or int(after) <= int(before), \
        "a rollback with no platform drift should not create a platform commit"


def test_report_lists_the_platform_files_it_reapplied(leonardo_repo):
    """Surfaced so the History panel can tell the customer what happened."""
    from app.services import checkpoint_service

    result = checkpoint_service.CheckpointService.rollback_to_checkpoint(
        leonardo_repo["checkpoint_sha"], report=True
    )
    assert isinstance(result, dict)
    assert result["success"] is True
    reapplied = set(result["platform_files_reapplied"])
    assert "bin/update" in reapplied
    assert "docker-compose.yml" in reapplied
    assert "langgraph/langgraph.json" in reapplied


def test_rollback_still_returns_true_by_default(leonardo_repo):
    """Existing callers (routers/checkpoints.py) must not change shape."""
    assert leonardo_repo["service"].rollback_to_checkpoint(leonardo_repo["checkpoint_sha"]) is True
