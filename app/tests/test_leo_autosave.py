"""LP-46: Leo saves the user's work on its own, and the History panel tells the truth.

A free user saw "669 unsaved changes" after ~10 weeks of building and asked for an
outside backup. Nothing was lost: auto-checkpointing had been switched off, so Leo
never committed, and the panel counted every file changed since the last commit as
"unsaved". Worse, the alarm invited a click on Discard, which deletes working code.

Now every Leo turn that changes files ends in one commit, secrets and uploads are
never in it, a failed save never breaks the turn, and every restore saves first.

Every test runs against a throwaway repo: the container has the live Leonardo repo
mounted, and a real auto-save there would commit the dev box's work.
"""
import asyncio
import logging
import subprocess
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import checkpoint_service as svc_mod
from app.services import git_setup

Service = svc_mod.CheckpointService


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _commits(repo):
    return int(_git(repo, "rev-list", "--count", "HEAD"))


def _files_in(repo, ref="HEAD"):
    return set(_git(repo, "show", ref, "--name-only", "--format=").split())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A Leonardo-shaped repo with one commit made by the box's owner, not Leo."""
    path = tmp_path / "leonardo"
    (path / "rails" / "app" / "models").mkdir(parents=True)
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(path)],
                   capture_output=True, timeout=5)
    _git(tmp_path, "init", "-q", str(path))
    _git(path, "config", "user.email", "owner@example.com")
    _git(path, "config", "user.name", "Owner")
    (path / "rails" / "app" / "models" / "widget.rb").write_text("class Widget; end\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "first")

    monkeypatch.setattr(svc_mod, "LEONARDO_PATH", path)
    monkeypatch.setattr(git_setup, "LEONARDO_PATH", path)
    monkeypatch.setattr(svc_mod, "_last_save_error", None)
    monkeypatch.setattr(svc_mod, "_has_leo_save", None)
    monkeypatch.setattr(svc_mod, "_backlog_pending", False)

    class _Session:  # the checkpoint DB row is not under test here
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, obj): self.obj = obj
        def commit(self): pass
        def refresh(self, obj): obj.created_at = datetime.now(timezone.utc)

    monkeypatch.setattr(svc_mod, "Session", _Session)
    return path


def _edit(repo, n=1, prefix="page"):
    for i in range(n):
        (repo / "rails" / "app" / "models" / f"{prefix}{i}.rb").write_text(f"class P{i}; end\n")


def _mark_box_as_already_auto_saving(repo):
    """Past the one-off backlog save, so turns get their own "Leo:" title."""
    _git(repo, "-c", "user.name=Leo", "-c", f"user.email={svc_mod.LEO_AUTHOR_EMAIL}",
         "commit", "-q", "--allow-empty", "-m", "Leo: earlier")


# ---------------------------------------------------------------- one commit per turn

def test_a_turn_that_changes_files_ends_in_one_leo_commit(repo):
    _mark_box_as_already_auto_saving(repo)
    before = _commits(repo)
    svc_mod.note_turn_start()
    _edit(repo, 3)

    result = svc_mod.autosave_after_turn("Add a pricing page with three tiers", thread_id="t1")

    assert result and not result.get("skipped")
    assert _commits(repo) == before + 1
    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject.startswith("Leo: Add a pricing page with three tiers · ")
    assert subject.endswith(" UTC")
    assert _git(repo, "log", "-1", "--format=%an <%ae>") == f"Leo <{svc_mod.LEO_AUTHOR_EMAIL}>"
    assert _git(repo, "status", "--porcelain") == ""


def test_a_turn_that_only_answers_makes_no_commit(repo):
    before = _commits(repo)
    svc_mod.note_turn_start()

    result = svc_mod.autosave_after_turn("What does this app do?", thread_id="t1")

    assert result is None or result.get("skipped")
    assert _commits(repo) == before


def test_twelve_edits_in_one_turn_are_one_commit(repo):
    _mark_box_as_already_auto_saving(repo)
    before = _commits(repo)
    svc_mod.note_turn_start()
    _edit(repo, 12)

    svc_mod.autosave_after_turn("Build the CRM", thread_id="t1")

    assert _commits(repo) == before + 1
    assert len(_files_in(repo)) == 12


def test_the_title_has_no_newlines_or_control_characters_and_stays_short(repo):
    title = svc_mod.turn_title("Make a\nlogin\x1b[31m page " + "x" * 200,
                               now=datetime(2026, 11, 10, 14, 32, tzinfo=timezone.utc))
    assert "\n" not in title and "\x1b" not in title
    assert title.startswith("Leo: Make a login [31m page ")
    assert title.endswith(" · 2026-11-10 14:32 UTC")
    assert len(title.split(" · ")[0]) <= len("Leo: ") + 60


def test_auto_save_works_on_a_box_with_no_git_identity(repo, tmp_path, monkeypatch):
    _git(repo, "config", "--unset", "user.email")
    _git(repo, "config", "--unset", "user.name")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(git_setup, "ensure_git_identity",
                        lambda: pytest.fail("auto-save must not need the box's identity"))
    _edit(repo)

    svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    assert _git(repo, "status", "--porcelain") == ""


# ---------------------------------------------------------------- the backlog

def test_the_first_auto_save_on_an_old_box_is_one_backlog_commit(repo):
    _edit(repo, 30, prefix="old")          # weeks of work nobody committed
    svc_mod.note_turn_start()
    _edit(repo, 2, prefix="new")           # this turn's edits

    svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    assert _git(repo, "log", "-1", "--format=%s") == svc_mod.BACKLOG_TITLE
    assert len(_files_in(repo)) == 32
    assert svc_mod.save_status()["changed_files"] == 0


def test_a_clean_first_turn_gets_a_normal_title(repo):
    svc_mod.note_turn_start()
    _edit(repo, 3)

    svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    assert _git(repo, "log", "-1", "--format=%s").startswith("Leo: Add a page")


def test_files_over_the_size_limit_are_left_out_and_logged(repo, monkeypatch, caplog):
    monkeypatch.setattr(svc_mod, "MAX_SAVE_FILE_BYTES", 1000)
    (repo / "rails" / "big.bin").write_bytes(b"0" * 5000)
    _edit(repo)

    with caplog.at_level(logging.WARNING):
        svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    assert "rails/big.bin" not in _files_in(repo)
    assert "rails/big.bin" in caplog.text


# ---------------------------------------------------------------- never commit secrets or uploads

SECRETS = [
    ".env", "rails/.env", "rails/.env.production", "rails/config/master.key",
    "rails/config/credentials/production.key", "certs/server.pem",
    "rails/storage/uploads/photo.png", "rails/log/development.log", "rails/tmp/cache/x",
]


def test_secrets_and_uploads_are_never_committed(repo, caplog):
    assert not (repo / ".gitignore").exists()  # nothing else is keeping them out
    for rel in SECRETS:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("SECRET=1\n")
    (repo / "rails" / ".env.example").write_text("SECRET=\n")
    _edit(repo)

    with caplog.at_level(logging.WARNING):
        svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    committed = _files_in(repo)
    for rel in SECRETS:
        assert rel not in committed, f"{rel} was committed"
        assert rel in caplog.text, f"{rel} was dropped without a log line"
    assert "rails/.env.example" in committed
    assert "rails/app/models/page0.rb" in committed


def test_a_secret_someone_already_staged_is_unstaged(repo):
    (repo / ".env").write_text("SECRET=1\n")
    _git(repo, "add", "-f", ".env")
    _edit(repo)

    svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    assert ".env" not in _files_in(repo)


def test_secrets_do_not_make_the_panel_say_unsaved(repo):
    (repo / ".env").write_text("SECRET=1\n")
    assert svc_mod.save_status()["clean"] is True


def test_the_manual_save_drops_secrets_too(repo):
    (repo / ".env").write_text("SECRET=1\n")
    _edit(repo)

    Service.create_checkpoint(thread_id="manual", description="my save")

    assert ".env" not in _files_in(repo)


# ---------------------------------------------------------------- a failed save

def _break_commits(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)


def test_a_failed_save_never_raises_and_the_panel_says_so(repo, caplog):
    _break_commits(repo)
    _edit(repo)

    with caplog.at_level(logging.ERROR):
        result = svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    assert result is None
    assert "Auto-save failed" in caplog.text
    status = svc_mod.save_status()
    assert status["clean"] is False
    assert status["last_save_error"]


def test_the_next_good_save_clears_the_error(repo):
    _break_commits(repo)
    _edit(repo)
    svc_mod.autosave_after_turn("Add a page", thread_id="t1")
    (repo / ".git" / "hooks" / "pre-commit").unlink()

    svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    assert svc_mod.save_status()["last_save_error"] is None
    assert svc_mod.save_status()["clean"] is True


def test_a_held_index_lock_is_retried_once(repo, monkeypatch):
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    real_sleep = svc_mod.time.sleep
    monkeypatch.setattr(svc_mod.time, "sleep", lambda s: (lock.unlink(missing_ok=True), real_sleep(0)))
    _edit(repo)

    svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    assert _git(repo, "status", "--porcelain") == ""


# ---------------------------------------------------------------- status

def test_status_reports_saved_with_the_last_commit_time(repo):
    status = svc_mod.save_status()

    assert status["clean"] is True
    assert status["changed_files"] == 0
    assert status["last_save_error"] is None
    assert status["turn_in_progress"] is False
    epoch = int(_git(repo, "log", "-1", "--format=%ct"))
    assert status["last_saved_at"] == datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_status_counts_changes_without_calling_them_unsaved(repo):
    _edit(repo, 4)
    status = svc_mod.save_status(turn_in_progress=True)
    assert (status["clean"], status["changed_files"], status["turn_in_progress"]) == (False, 4, True)


# ---------------------------------------------------------------- manual + auto at once

def test_manual_save_and_auto_save_at_once_make_one_commit(repo):
    _mark_box_as_already_auto_saving(repo)
    before = _commits(repo)
    _edit(repo, 5)
    errors = []
    start = threading.Barrier(2)

    def auto():
        start.wait()
        svc_mod.autosave_after_turn("Add a page", thread_id="t1")

    def manual():
        start.wait()
        try:
            Service.create_checkpoint(thread_id="manual", description="Save now", skip_if_clean=True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=auto), threading.Thread(target=manual)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == []
    assert _commits(repo) == before + 1
    assert len(_files_in(repo)) == 5


# ---------------------------------------------------------------- restore is reversible

def test_restore_saves_the_current_state_first_and_can_be_undone(repo):
    target = _git(repo, "rev-parse", "HEAD")
    _edit(repo, 2, prefix="later")

    result = Service.restore_checkpoint(target)

    assert result["success"] is True
    saved = result["saved_before_restore"]
    assert saved, "the current state was not saved first"
    assert _git(repo, "log", "-1", "--format=%s", saved) == "Before going back to first"
    assert not (repo / "rails" / "app" / "models" / "later0.rb").exists()

    Service.restore_checkpoint(saved)
    assert (repo / "rails" / "app" / "models" / "later0.rb").exists()


def test_the_pre_restore_save_stays_in_the_history_list(repo):
    """reset --hard leaves the save on no branch; the panel lists `git log --all`."""
    target = _git(repo, "rev-parse", "HEAD")
    _edit(repo, 2, prefix="later")

    saved = Service.restore_checkpoint(target)["saved_before_restore"]

    graph = Service.get_git_graph()
    listed = {c["sha"]: c for c in graph["commits"]}
    assert saved in listed, "the way back is not in the History list"
    assert not any(r.startswith("refs/llamabot/") for r in listed[saved]["refs"]), \
        "internal pin ref shown as a badge"


def test_restore_from_a_clean_tree_adds_no_save(repo):
    target = _git(repo, "rev-parse", "HEAD")
    _edit(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    before = _commits(repo)

    result = Service.restore_checkpoint(target)

    assert result["saved_before_restore"] is None
    assert _git(repo, "rev-list", "--count", "HEAD@{1}") == str(before)


def test_restore_is_refused_when_the_save_first_fails(repo):
    target = _git(repo, "rev-parse", "HEAD")
    _break_commits(repo)
    _edit(repo)

    with pytest.raises(Exception):
        Service.restore_checkpoint(target)
    assert (repo / "rails" / "app" / "models" / "page0.rb").exists()


# ---------------------------------------------------------------- the end-of-turn hook

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _manager_with_hook(calls):
    from app.websocket.run_manager import RunManager
    return RunManager(after_turn=lambda request, thread_id: calls.append((request, thread_id)) or {"saved": True})


def _frame(text, thread="t1"):
    from app.websocket.run_manager import QueuedMessage
    return QueuedMessage({"thread_id": thread, "message": text})


def test_the_hook_runs_once_after_the_reply_is_sent():
    calls, order = [], []
    rm = _manager_with_hook(calls)

    async def go():
        async def factory(sink):
            await sink.send_json({"type": "end"})
            order.append("reply")
        await rm.submit("t1", _frame("Add a page"), lambda items: factory)
        await rm.get("t1").task

    _run(go())
    assert calls == [("Add a page", "t1")]
    log_types = [e["type"] for e in rm.get("t1").log.since(0)]
    assert log_types.index("end") < log_types.index("save_status")


def test_the_hook_runs_when_the_turn_errors_out():
    calls = []
    rm = _manager_with_hook(calls)

    async def go():
        async def factory(sink):
            raise RuntimeError("model died")
        await rm.submit("t1", _frame("Add a page"), lambda items: factory)
        await rm.get("t1").task

    _run(go())
    assert len(calls) == 1


def test_a_hook_that_raises_never_breaks_the_turn():
    from app.websocket.run_manager import RunManager

    def boom(request, thread_id):
        raise RuntimeError("git exploded")

    rm = RunManager(after_turn=boom)

    async def go():
        async def factory(sink):
            await sink.send_json({"type": "end"})
        await rm.submit("t1", _frame("Add a page"), lambda items: factory)
        await rm.get("t1").task

    _run(go())
    assert rm.get("t1").log.status == "done"


def test_a_notification_turn_keeps_the_user_request_it_belongs_to():
    from app.websocket.run_manager import QueuedMessage
    calls = []
    rm = _manager_with_hook(calls)

    async def go():
        async def factory(sink):
            pass
        await rm.submit("t1", _frame("Build the CRM"), lambda items: factory)
        await rm.get("t1").task
        await rm.submit("t1", QueuedMessage({"thread_id": "t1", "message": "<task-notification>"},
                                            notification=True), lambda items: factory)
        await rm.get("t1").task

    _run(go())
    assert calls == [("Build the CRM", "t1"), ("Build the CRM", "t1")]


def test_busy_means_any_turn_or_coding_task_is_running():
    from app.websocket.run_manager import RunManager
    rm = RunManager()
    assert rm.busy() is False

    async def go():
        gate = asyncio.Event()

        async def factory(sink):
            await gate.wait()
        await rm.submit("t1", _frame("x"), lambda items: factory)
        during = rm.busy()
        gate.set()
        await rm.get("t1").task
        return during

    assert _run(go()) is True
    assert rm.busy() is False


def test_the_app_run_manager_has_the_auto_save_hook():
    from app.websocket import run_manager
    assert run_manager.shared_run_manager()._after_turn is run_manager.autosave_hook


def test_the_hook_is_off_under_pytest_so_tests_never_commit_the_live_repo(monkeypatch):
    from app.websocket import run_manager
    called = []
    monkeypatch.setattr(svc_mod, "autosave_after_turn", lambda *a, **k: called.append(a))
    assert run_manager.autosave_hook("x", "t1") is None
    assert called == []


def test_the_hook_skips_while_a_coding_task_is_editing(monkeypatch):
    from app.websocket import run_manager
    called = []
    monkeypatch.delenv("LEO_AUTOSAVE_DISABLED", raising=False)
    monkeypatch.setattr(svc_mod, "autosave_after_turn", lambda *a, **k: called.append(a))
    monkeypatch.setattr(run_manager.shared_run_manager(), "writers_busy", lambda: True)
    run_manager.autosave_hook("x", "t1")
    assert called == []


# ---------------------------------------------------------------- the routes

def _client(monkeypatch, rm=None):
    from fastapi.testclient import TestClient
    from main import app
    from app.dependencies import get_current_user
    from unittest.mock import MagicMock
    if rm is not None:
        monkeypatch.setattr(app.state, "run_manager", rm, raising=False)
    app.dependency_overrides[get_current_user] = lambda: MagicMock()
    return TestClient(app), app


def test_status_endpoint_shape_and_turn_in_progress(repo, monkeypatch):
    rm = SimpleNamespace(busy=lambda: True, writers_busy=lambda: False)
    client, app = _client(monkeypatch, rm)
    try:
        body = client.get("/api/checkpoints/status").json()
    finally:
        app.dependency_overrides = {}
    assert set(body) == {"clean", "last_saved_at", "turn_in_progress", "changed_files", "last_save_error",
                         "autosave_enabled"}  # LP-49
    assert body["turn_in_progress"] is True


def test_rollback_endpoint_saves_first(repo, monkeypatch):
    target = _git(repo, "rev-parse", "HEAD")
    _edit(repo)
    monkeypatch.setattr(Service, "mark_checkpoint_rejected", staticmethod(lambda cid: True))
    client, app = _client(monkeypatch)
    try:
        body = client.post(f"/api/checkpoints/{target}/rollback").json()
    finally:
        app.dependency_overrides = {}
    assert body["success"] is True
    assert body["saved_before_restore"]


def test_write_access_to_checkpoints_is_unchanged():
    from app import permissions
    security_only = SimpleNamespace(is_admin=False, is_security_admin=True, is_system_admin=False)
    assert permissions.route_allowed(security_only, "POST", "/api/checkpoints", zdr_enforced=False) is False
    assert permissions.route_allowed(security_only, "GET", "/api/checkpoints/status", zdr_enforced=False) is True
