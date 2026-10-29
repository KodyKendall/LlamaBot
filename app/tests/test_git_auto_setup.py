"""LP-20: Save and Push set git up themselves instead of showing raw git errors.

mbc-preceptors (2026-10-27): the History panel's Save answered with a 500 and
"Git commit failed: Author identity unknown ... Run git config --global
user.email", and Push answered "could not read Username ...: terminal prompts
disabled". The fix for each was an undocumented slash command (/git-config,
/gh-copy, /gh) that nothing on screen mentioned.

The commit tests use a REAL git repo with HOME pointed at an empty directory, so
"no identity" is genuinely git's opinion and not a mocked string. The push tests
fake only the subprocess call, since there is no remote to push to.
"""
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import checkpoints as router
from app.services import checkpoint_service as svc_mod
from app.services import git_setup


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo on a box where git knows nobody, and a code container we record."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    # git falls back to the passwd entry + hostname for a name; an empty GECOS on
    # root still leaves the email undetectable, which is the error we need.
    path = tmp_path / "leonardo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "app.rb").write_text("puts 1\n")
    monkeypatch.setattr(svc_mod, "LEONARDO_PATH", path)
    monkeypatch.setattr(git_setup, "LEONARDO_PATH", path)

    host_cmds = []
    monkeypatch.setattr(
        git_setup, "_run_on_host",
        lambda cmd, timeout=60: host_cmds.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    class _Session:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, obj): self.obj = obj
        def commit(self): pass
        def refresh(self, obj):
            from datetime import datetime, timezone
            obj.created_at = datetime.now(timezone.utc)

    monkeypatch.setattr(svc_mod, "Session", _Session)
    return SimpleNamespace(path=path, home=home, host_cmds=host_cmds)


def test_the_box_really_has_no_identity(repo):
    """Guards the fixture: without this the next test could pass for free."""
    _git(repo.path, "add", ".")
    result = _git(repo.path, "commit", "-m", "x")
    assert result.returncode != 0
    assert git_setup.is_missing_identity(result.stderr), result.stderr


# ---------------------------------------------------------------------------
# FR-1: a missing identity is set and the commit retried
# ---------------------------------------------------------------------------

def test_save_sets_up_the_identity_and_succeeds(repo):
    checkpoint = svc_mod.CheckpointService.create_checkpoint("t1", "first save")

    assert checkpoint["checkpoint_id"] == _git(repo.path, "rev-parse", "HEAD").stdout.strip()
    author = _git(repo.path, "log", "-1", "--format=%an <%ae>").stdout.strip()
    assert author == f"{git_setup.GIT_USER_NAME} <{git_setup.GIT_USER_EMAIL}>"


def test_the_code_container_gets_the_identity_too(repo):
    svc_mod.CheckpointService.create_checkpoint("t1", "first save")
    sent = " ".join(repo.host_cmds)
    assert "exec -T code" in sent and "user.email" in sent
    assert " -it " not in sent, "no TTY here — /git-config's -it is why it never ran headless"


def test_an_existing_identity_is_never_overwritten(repo):
    _git(repo.path, "config", "--global", "user.name", "Kody")
    svc_mod.CheckpointService.create_checkpoint("t1", "save")

    assert _git(repo.path, "config", "--global", "user.name").stdout.strip() == "Kody"
    assert _git(repo.path, "config", "--global", "user.email").stdout.strip() == git_setup.GIT_USER_EMAIL
    assert _git(repo.path, "log", "-1", "--format=%an").stdout.strip() == "Kody"


def test_a_configured_box_never_touches_git_config(repo, monkeypatch):
    _git(repo.path, "config", "--global", "user.name", "Kody")
    _git(repo.path, "config", "--global", "user.email", "kody@example.com")
    monkeypatch.setattr(git_setup, "ensure_git_identity",
                        lambda: pytest.fail("identity setup ran on a box that had one"))
    svc_mod.CheckpointService.create_checkpoint("t1", "save")


# ---------------------------------------------------------------------------
# FR-4: what reaches the user is one plain sentence, never stderr
# ---------------------------------------------------------------------------

def test_an_unrecoverable_save_says_one_plain_sentence(repo):
    hook = repo.path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'fatal: SECRET-STDERR' >&2\nexit 1\n")
    hook.chmod(0o755)

    with pytest.raises(HTTPException) as err:
        router.create_checkpoint(router.CreateCheckpointRequest(description="x"),
                                 SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    assert err.value.status_code == 500
    assert "SECRET-STDERR" not in err.value.detail
    assert "git" not in err.value.detail.lower()
    assert err.value.detail.endswith(".")


# ---------------------------------------------------------------------------
# FR-2 / FR-3: push syncs credentials, retries once, then asks for sign-in
# ---------------------------------------------------------------------------

AUTH_FAIL = ("fatal: could not read Username for 'https://github.com': "
             "terminal prompts disabled")


@pytest.fixture
def push(monkeypatch):
    """Scripted `git push` results, plus a recorder for the credential sync."""
    state = SimpleNamespace(results=[], pushes=0, syncs=0)

    def fake_run(cmd, **kw):
        assert cmd[-1] == "push", cmd
        state.pushes += 1
        rc, err = state.results.pop(0)
        return SimpleNamespace(returncode=rc, stdout="", stderr=err)

    def fake_sync():
        state.syncs += 1
        return True

    monkeypatch.setattr(router.subprocess, "run", fake_run)
    monkeypatch.setattr(git_setup, "sync_gh_credentials", fake_sync)
    monkeypatch.delenv("ENABLE_GITHUB_BUTTON", raising=False)
    return state


def _body(response):
    import json
    return json.loads(response.body)


def test_a_push_auth_failure_syncs_credentials_and_retries_once(push):
    push.results = [(128, AUTH_FAIL), (0, "")]
    body = _body(router.git_push())
    assert body["success"] is True
    assert (push.pushes, push.syncs) == (2, 1)


@pytest.mark.parametrize("stderr", [
    AUTH_FAIL,
    "remote: Invalid username or password.\nfatal: Authentication failed for 'https://github.com/x/y.git/'",
    "git@github.com: Permission denied (publickey).",
    "remote: Permission to x/y.git denied to bot.\nfatal: unable to access '...': The requested URL returned error: 403",
])
def test_every_auth_failure_shape_is_recognised(stderr):
    assert git_setup.is_push_auth_failure(stderr)


def test_a_still_unauthenticated_push_asks_for_github_sign_in(push, monkeypatch):
    monkeypatch.setenv("ENABLE_GITHUB_BUTTON", "true")
    push.results = [(128, AUTH_FAIL), (128, AUTH_FAIL)]
    body = _body(router.git_push())
    assert body["success"] is False
    assert body["needs_github_auth"] is True
    assert (push.pushes, push.syncs) == (2, 1), "retried more than once"
    assert "terminal prompts" not in body["message"]


def test_without_the_github_button_there_is_no_modal_just_a_sentence(push):
    push.results = [(128, AUTH_FAIL), (128, AUTH_FAIL)]
    body = _body(router.git_push())
    assert body["success"] is False
    assert body["needs_github_auth"] is False
    assert "Username" not in body["message"] and body["message"].endswith(".")


def test_a_non_auth_push_failure_does_not_touch_credentials(push):
    push.results = [(1, " ! [rejected]        main -> main (fetch first)\nerror: failed to push some refs")]
    body = _body(router.git_push())
    assert body["success"] is False
    assert push.syncs == 0
    assert "rejected" not in body["message"] and "refs" not in body["message"]


# ---------------------------------------------------------------------------
# One copy of the /gh-copy sequence
# ---------------------------------------------------------------------------

def test_slash_command_and_sign_in_share_the_credential_copy():
    from app.routers.slash_commands import SLASH_COMMANDS

    assert git_setup.GH_COPY_COMMAND in SLASH_COMMANDS["gh-copy"]["command"]
    assert " -it " not in git_setup.GH_COPY_COMMAND
