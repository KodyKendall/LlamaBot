"""Discard can wipe a never-committed app (0.7.15, LP-7).

A Starter customer (box leo-note) had never made a checkpoint, so "uncommitted
changes" was his whole app: 430 files, 22 controllers, 26 models. Discard asked
the same one-line question it asks for one edited file, the app went to a 404,
and the only Undo sat inside the closed checkpoint panel. The snapshot worked;
the warning and the way back did not.

So: get_uncommitted_changes() now says how much of the app is at stake, and a
discard leaves a "pending" marker that keeps the chat's Undo card up until the
user undoes or dismisses it.
"""

import pytest

from tests.test_checkpoint_discard_backup import _client, _git, leonardo_repo, signed_in  # noqa: F401


def _write(repo, rel, text="x\n"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------- counts

def test_counts_name_what_part_of_the_app_is_at_stake(leonardo_repo):
    repo = leonardo_repo["path"]
    leonardo_repo["app_file"].write_text("class Widget; V = 2; end\n")  # modified model
    _write(repo, "rails/app/models/player.rb")
    _write(repo, "rails/app/controllers/players_controller.rb")
    _write(repo, "rails/app/controllers/concerns/auth.rb")
    _write(repo, "rails/app/views/players/index.html.erb")
    _write(repo, "rails/app/views/players/show.html.erb")
    _write(repo, "rails/public/logo.png")

    changes = leonardo_repo["service"].get_uncommitted_changes()

    assert changes["total_count"] == 7
    assert changes["controller_count"] == 2
    assert changes["model_count"] == 2
    assert changes["view_count"] == 2
    assert changes["untracked_controllers"] is True
    assert changes["untracked_models"] is True


def test_editing_committed_app_code_is_not_flagged_as_never_committed(leonardo_repo):
    leonardo_repo["app_file"].write_text("class Widget; V = 2; end\n")

    changes = leonardo_repo["service"].get_uncommitted_changes()

    assert changes["total_count"] == 1
    assert changes["model_count"] == 1
    assert changes["untracked_models"] is False
    assert changes["untracked_controllers"] is False


def test_a_clean_tree_reports_zero_counts(leonardo_repo):
    changes = leonardo_repo["service"].get_uncommitted_changes()
    assert (changes["controller_count"], changes["model_count"], changes["view_count"]) == (0, 0, 0)


# ---------------------------------------------------------------- pending undo

def _discard_new_model(repo_info, name="invoice"):
    _write(repo_info["path"], f"rails/app/models/{name}.rb")
    return repo_info["service"].discard_uncommitted_changes()


def test_a_discard_leaves_its_undo_pending(leonardo_repo):
    result = _discard_new_model(leonardo_repo)

    backups = leonardo_repo["service"].list_discard_backups()
    assert backups[0]["ref"] == result["backup_ref"]
    assert backups[0]["pending"] is True


def test_undo_clears_the_pending_card(leonardo_repo):
    service = leonardo_repo["service"]
    _discard_new_model(leonardo_repo)

    assert service.restore_discarded_changes()["success"] is True
    assert service.list_discard_backups()[0]["pending"] is False
    assert (leonardo_repo["path"] / "rails/app/models/invoice.rb").exists()


def test_dismiss_clears_the_card_but_keeps_the_backup(leonardo_repo):
    service = leonardo_repo["service"]
    result = _discard_new_model(leonardo_repo)

    assert service.dismiss_discard_backup(result["backup_ref"])["success"] is True

    backups = service.list_discard_backups()
    assert backups[0]["pending"] is False
    # The snapshot itself is never thrown away: the panel's Undo still works.
    assert _git(leonardo_repo["path"], "rev-parse", "--verify", result["backup_ref"])
    assert service.restore_discarded_changes()["success"] is True


def test_dismissing_one_discard_leaves_a_newer_one_pending(leonardo_repo):
    service = leonardo_repo["service"]
    older = _discard_new_model(leonardo_repo, "a")
    newer = _discard_new_model(leonardo_repo, "b")

    service.dismiss_discard_backup(older["backup_ref"])

    pending = {b["ref"]: b["pending"] for b in service.list_discard_backups()}
    assert pending == {older["backup_ref"]: False, newer["backup_ref"]: True}


def test_backups_from_before_this_release_never_raise_a_card(leonardo_repo):
    """Old snapshots (support may already have applied them by hand) have no marker."""
    repo = leonardo_repo["path"]
    result = _discard_new_model(leonardo_repo)
    pending_ref = result["backup_ref"].replace("refs/llamabot/discards/", "refs/llamabot/discard-pending/")
    _git(repo, "update-ref", "-d", pending_ref)

    assert leonardo_repo["service"].list_discard_backups()[0]["pending"] is False


def test_dismiss_rejects_refs_outside_the_backup_namespace(leonardo_repo):
    repo = leonardo_repo["path"]
    branch = _git(repo, "symbolic-ref", "HEAD")

    result = leonardo_repo["service"].dismiss_discard_backup(branch)

    assert result["success"] is False
    assert _git(repo, "rev-parse", "--verify", branch)


def test_dismiss_endpoint(monkeypatch):
    import app.routers.checkpoints as checkpoints

    seen = {}

    def fake_dismiss(backup_ref):
        seen["ref"] = backup_ref
        return {"success": True}

    monkeypatch.setattr(
        checkpoints.checkpoint_service, "dismiss_discard_backup", staticmethod(fake_dismiss)
    )
    resp = _client().post("/api/checkpoints/discard/dismiss", json={"backup_ref": "refs/llamabot/discards/x"})

    assert resp.status_code == 200
    assert seen["ref"] == "refs/llamabot/discards/x"
