"""LP-49: auto-save is ON by default, and a user can turn it off.

LP-46 commits after every Leo turn. Developers who curate their own Git history
(a box whose main branch is merged into production) got "Leo:" commits in their
pull requests, and the only way out was LEO_AUTOSAVE_DISABLED=1 in .env plus a
container recreate. Now it is a site setting, read on every turn.

Rules pinned here:
- no row = ON, and nothing writes a row at boot (the default stays in code);
- only the exact value "false" turns it off; anything unreadable is ON;
- LEO_AUTOSAVE_DISABLED=1 (the test suite's switch) still wins;
- OFF stops end-of-turn commits only: manual Save and save-before-restore still work;
- turning it back ON saves everything pending in one commit at the end of the next turn.

Repo tests run against the throwaway repo from test_leo_autosave, never the live one.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.services import checkpoint_service as svc_mod
from app.tests.test_leo_autosave import (  # noqa: F401 - `repo` is a fixture
    _commits, _edit, _files_in, _git, _mark_box_as_already_auto_saving, repo,
)

KEY = "leo_autosave_enabled"


@pytest.fixture
def db(monkeypatch):
    """A private auth DB standing in for app.db.engine. Auto-save reads it live."""
    import app.models  # noqa: F401 - register tables
    from app import db as db_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.delenv("LEO_AUTOSAVE_DISABLED", raising=False)
    return engine


def _set(engine, value):
    from app.models import SiteSetting
    with Session(engine) as s:
        row = s.get(SiteSetting, KEY)
        if row:
            row.value = value
        else:
            s.add(SiteSetting(key=KEY, value=value))
        s.commit()


def _rows(engine):
    from app.models import SiteSetting
    with Session(engine) as s:
        return s.exec(select(SiteSetting).where(SiteSetting.key == KEY)).all()


@pytest.fixture
def hook(monkeypatch):
    """Run the real end-of-turn hook, with no coding task holding the repo."""
    from app.websocket import run_manager
    monkeypatch.setattr(run_manager.shared_run_manager(), "writers_busy", lambda: False)

    def turn(request="Edit the pricing page"):
        run_manager.autosave_turn_start_hook()
        return run_manager.autosave_hook(request, "t1")
    return turn


# ---------------------------------------------------------------- reading the setting

def test_no_row_means_on_and_reading_writes_no_row(db):
    assert svc_mod.autosave_enabled() is True
    assert _rows(db) == []


@pytest.mark.parametrize("value, on", [
    ("false", False), ("true", True), ("", True), ("False ", False), ("garbage", True), ("0", True),
])
def test_only_false_turns_it_off(db, value, on):
    _set(db, value)
    assert svc_mod.autosave_enabled() is on


def test_an_unreadable_database_means_on(monkeypatch):
    from app import db as db_mod
    monkeypatch.delenv("LEO_AUTOSAVE_DISABLED", raising=False)

    class Broken:
        def connect(self, *a, **k):
            raise RuntimeError("db down")
    monkeypatch.setattr(db_mod, "engine", Broken())
    assert svc_mod.autosave_enabled() is True

    monkeypatch.setattr(db_mod, "engine", None)  # no auth DB configured at all
    assert svc_mod.autosave_enabled() is True


def test_the_env_override_still_wins(db, monkeypatch):
    _set(db, "true")
    monkeypatch.setenv("LEO_AUTOSAVE_DISABLED", "1")
    assert svc_mod.autosave_enabled() is False


def test_save_status_reports_it(repo, db):
    assert svc_mod.save_status()["autosave_enabled"] is True
    _set(db, "false")
    assert svc_mod.save_status()["autosave_enabled"] is False


# ---------------------------------------------------------------- what a turn does

def test_a_new_box_saves_by_default(repo, db, hook):
    _mark_box_as_already_auto_saving(repo)
    before = _commits(repo)
    _edit(repo, 2)
    hook()
    assert _commits(repo) == before + 1


def test_off_means_no_commit_and_takes_effect_on_the_next_turn(repo, db, hook):
    _mark_box_as_already_auto_saving(repo)
    before = _commits(repo)
    _edit(repo, 1, prefix="a")
    hook()
    assert _commits(repo) == before + 1  # ON

    _set(db, "false")  # same process: no restart
    _edit(repo, 2, prefix="b")
    assert hook() is None
    assert _commits(repo) == before + 1
    assert svc_mod.save_status()["clean"] is False


def test_turning_it_back_on_saves_all_pending_work_in_one_commit(repo, db, hook):
    _mark_box_as_already_auto_saving(repo)
    _set(db, "false")
    for turn in range(3):
        _edit(repo, 2 if turn < 2 else 1, prefix=f"t{turn}_")
        hook()
    before = _commits(repo)
    assert len(_git(repo, "status", "--porcelain").splitlines()) == 5

    _set(db, "true")
    assert _commits(repo) == before  # switching on is not itself a save
    hook()

    assert _commits(repo) == before + 1
    assert len(_files_in(repo)) == 5
    assert _git(repo, "status", "--porcelain") == ""


def test_manual_save_still_works_when_off(repo, db):
    _set(db, "false")
    before = _commits(repo)
    _edit(repo, 2)
    svc_mod.CheckpointService.create_checkpoint(thread_id="t1", description="My save")
    assert _commits(repo) == before + 1


def test_restore_still_saves_first_when_off(repo, db):
    _set(db, "false")
    target = _git(repo, "rev-parse", "HEAD")
    _edit(repo, 2, prefix="later")
    result = svc_mod.CheckpointService.restore_checkpoint(target)
    assert result["success"] is True
    assert result["saved_before_restore"], "the current state was not saved first"


# ---------------------------------------------------------------- the API + permissions

def _client(engine, role="engineer", is_admin=False):
    from app.dependencies import auth, get_current_user, get_db_session
    from app.models import User
    from app.routers.api import router

    app = FastAPI()
    app.include_router(router)

    def _session():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[auth] = lambda: "dev"
    app.dependency_overrides[get_current_user] = lambda: User(
        username="dev", password_hash="x", is_admin=is_admin, role=role)
    return TestClient(app)


def test_the_setting_reads_as_on_when_there_is_no_row(db):
    body = _client(db).get(f"/api/site-settings/{KEY}").json()
    assert body == {"key": KEY, "value": "true"}
    assert _rows(db) == []


def test_an_engineer_can_turn_it_off_and_on(db):
    client = _client(db)
    assert client.put(f"/api/site-settings/{KEY}", json={"value": "false"}).status_code == 200
    assert svc_mod.autosave_enabled() is False
    assert client.put(f"/api/site-settings/{KEY}", json={"value": "true"}).status_code == 200
    assert svc_mod.autosave_enabled() is True


def test_a_role_that_cannot_save_checkpoints_cannot_change_it(db):
    from app import permissions
    response = _client(db, role="user").put(f"/api/site-settings/{KEY}", json={"value": "false"})
    assert response.status_code == 403
    assert _rows(db) == []

    # The same role gate as checkpoint writes, at the middleware layer.
    security_only = SimpleNamespace(is_admin=False, is_security_admin=True, is_system_admin=False)
    for path in (f"/api/site-settings/{KEY}", "/api/checkpoints"):
        method = "PUT" if "site-settings" in path else "POST"
        assert permissions.route_allowed(security_only, method, path, zdr_enforced=False) is False


# ---------------------------------------------------------------- settings page + telemetry

def test_the_settings_row_has_the_label_help_text_and_state():
    from app.routers.ui import autosave_setting_row
    on = autosave_setting_row(enabled=True)
    assert "Save Leo&#x27;s changes automatically" in on or "Save Leo's changes automatically" in on
    assert "After each Leo turn, your code is saved as a Git commit." in on
    assert "Turn this off if you manage your own Git commits." in on
    assert 'role="switch"' in on and 'aria-checked="true"' in on and " checked" in on
    assert "/api/site-settings/leo_autosave_enabled" in on

    off = autosave_setting_row(enabled=False)
    assert 'aria-checked="false"' in off and " checked" not in off.split("<input", 1)[1].split(">", 1)[0]


@pytest.mark.asyncio
async def test_report_health_says_whether_auto_save_is_on(db, monkeypatch):
    from unittest.mock import AsyncMock, patch
    from app.services.mothership_client import TELEMETRY_DISABLED_ENV
    from app.tests.test_rails_health_report import _make_client, _mock_response

    monkeypatch.delenv(TELEMETRY_DISABLED_ENV, raising=False)  # httpx is patched below

    _set(db, "false")
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        return _mock_response(body={"ok": True})

    ctx = AsyncMock()
    ctx.__aenter__.return_value.post = fake_post
    with patch("httpx.AsyncClient", return_value=ctx):
        await _make_client().report_rails_health(rails_status=200, rails_ms=10)
    assert captured["payload"]["autosave_enabled"] is False
