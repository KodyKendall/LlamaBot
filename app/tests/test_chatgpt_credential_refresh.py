"""A ChatGPT-plan connection must not silently go stale (0.7.8).

The incident (agent_task 323, InstanceError 5729/5716 on instance 2461, box
leo-zuset, 2026-09-09): Richie — a paying Business customer — sent six messages over
34 minutes and got no answer to any of them. The fifth was "How do I sign out???".
Every turn died with:

    AuthenticationError: 401 {'message': 'Provided authentication token is expired.
    Please try signing in again.', 'code': 'token_expired'}

His CLI credential (`.leonardo/chatgpt-auth/1/auth.json`) had been byte-for-byte
untouched for 11.3 days.

The mechanism, which is NOT "nothing ever tries to refresh":

  * `save_credential` stamps `expires_at = now + expires_in` (default 3600), so
    `_needs_refresh` goes true within the hour and a refresh IS attempted every turn.
  * `refresh_via_cli` runs `codex login status`, then **ignores the exit code and
    unconditionally returns whatever auth.json already says**.
  * So a refresh that did nothing looks like a refresh that worked. The SAME expired
    access token is handed back, `save_credential` re-stamps it with a fresh
    `expires_at`, and `_needs_refresh` reports "fine" for another hour.
  * That expired token then goes to chatgpt.com and 401s — while auth.json's mtime
    never moves, which is exactly what was observed.

A refresh that returns the same token is a FAILED refresh. Failing it makes
`access_token_for_user_sync` return None, and `get_llm` then falls open to the
operator's default model — the customer keeps working instead of being stranded.

Second guard here: `expires_at` is derived from the access token's own `exp` claim
when it has one, so an already-expired token can never be recorded as fresh no
matter what a caller passes for `expires_in`.

Blast radius at the time of filing: 3,624 messages across 12 boxes in 7 days, 9 of
them external and 7 on paid plans. leo-rofme had already logged its own 401.
"""

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services import codex_cli_auth as cli


def _jwt(exp_dt) -> str:
    """A JWT whose payload carries an `exp`. Only the payload segment is read."""
    payload = json.dumps({"exp": int(exp_dt.timestamp())}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"header.{body}.signature"


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CODEX_STATE_ROOT", tmp_path / "chatgpt-auth")
    return tmp_path / "chatgpt-auth"


def _write_auth(user_id, access_token):
    home = cli.codex_home_for_user(user_id)
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": access_token}}))


class _FakeProc:
    def __init__(self, returncode=0, output=b""):
        self.returncode = returncode
        self._output = output

    async def communicate(self):
        return (self._output, b"")


def _stub_cli(monkeypatch, *, proc, on_run=None):
    """Make refresh_via_cli think the CLI exists, and control what running it does."""
    monkeypatch.setattr(cli, "cli_available", lambda: True)
    monkeypatch.setattr(cli, "login_finished", lambda _uid: True)

    async def fake_exec(*args, **kwargs):
        if on_run:
            on_run()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


# --- refresh_via_cli: a no-op is a failure -----------------------------------


def test_a_refresh_that_changed_nothing_is_not_a_refresh(state_root, monkeypatch):
    """THE BUG. codex login status succeeded but the token is the one we already
    had, so nothing was refreshed. Returning it re-stamps a dead token as fresh."""
    _write_auth(1, "STALE")
    _stub_cli(monkeypatch, proc=_FakeProc(returncode=0))

    result = asyncio.run(cli.refresh_via_cli(1, previous_access_token="STALE"))

    assert result is None, (
        "an unchanged access token was reported as a successful refresh — this is "
        "what let an expired credential 401 for 11.3 days"
    )


def test_a_genuinely_refreshed_token_is_returned(state_root, monkeypatch):
    _write_auth(1, "STALE")

    def rotate():
        _write_auth(1, "FRESH")

    _stub_cli(monkeypatch, proc=_FakeProc(returncode=0), on_run=rotate)

    result = asyncio.run(cli.refresh_via_cli(1, previous_access_token="STALE"))

    assert result["tokens"]["access_token"] == "FRESH"


def test_a_nonzero_exit_is_a_failure(state_root, monkeypatch):
    """The exit code was thrown away entirely. A CLI that cannot reach
    auth.openai.com must read as "no usable credential", not as success."""
    _write_auth(1, "STALE")
    _stub_cli(monkeypatch, proc=_FakeProc(returncode=1, output=b"not logged in"))

    assert asyncio.run(cli.refresh_via_cli(1, previous_access_token="STALE")) is None


def test_no_previous_token_still_accepts_whatever_the_cli_has(state_root, monkeypatch):
    """First use after a connect: there is nothing to compare against, and the
    credential on disk is the one we want."""
    _write_auth(1, "FIRST")
    _stub_cli(monkeypatch, proc=_FakeProc(returncode=0))

    result = asyncio.run(cli.refresh_via_cli(1, previous_access_token=None))

    assert result["tokens"]["access_token"] == "FIRST"


# --- expires_at comes from the token, not from a hopeful default -------------


def test_expiry_is_read_from_the_token_itself():
    from app.services.chatgpt_auth import expires_at_for_token

    real = datetime.now(timezone.utc) + timedelta(minutes=42)
    got = expires_at_for_token(_jwt(real), expires_in=3600)

    # The token's own exp wins over the caller's guess.
    assert abs((got - real).total_seconds()) < 2


def test_an_already_expired_token_is_never_recorded_as_fresh():
    """The re-stamping half of the bug: whatever expires_in says, a token that is
    already dead must land in the past so _needs_refresh keeps saying "refresh"."""
    from app.services.chatgpt_auth import expires_at_for_token

    dead = datetime.now(timezone.utc) - timedelta(hours=5)
    got = expires_at_for_token(_jwt(dead), expires_in=3600)

    assert got < datetime.now(timezone.utc)


def test_a_token_with_no_exp_falls_back_to_expires_in():
    from app.services.chatgpt_auth import expires_at_for_token

    got = expires_at_for_token("not-a-jwt", expires_in=60)
    delta = (got - datetime.now(timezone.utc)).total_seconds()

    assert 50 < delta <= 61


def test_a_missing_token_still_yields_a_usable_expiry():
    from app.services.chatgpt_auth import expires_at_for_token

    got = expires_at_for_token(None, expires_in=None)
    assert got > datetime.now(timezone.utc) - timedelta(seconds=1)
