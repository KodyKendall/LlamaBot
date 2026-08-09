"""Codex-CLI-delegated sign-in: isolation, parsing, and cleanup.

The network path is covered by manual QA (it needs a real ChatGPT approval).
What is pinned here is everything that can silently cross users or leak: the
per-user CODEX_HOME, the scrubbed environment, and that disconnect really
forgets the credential.
"""

import asyncio

import pytest

from app.services import codex_cli_auth as cli


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CODEX_STATE_ROOT", tmp_path / "chatgpt-auth")
    return tmp_path / "chatgpt-auth"


# --- per-user isolation ------------------------------------------------------


def test_each_user_gets_their_own_codex_home(state_root):
    """One user's login must never read or overwrite another's."""
    a = cli.codex_home_for_user(1)
    b = cli.codex_home_for_user(2)

    assert a != b
    assert a.is_dir() and b.is_dir()
    assert cli.auth_json_path(1) != cli.auth_json_path(2)


def test_codex_home_is_private(state_root):
    home = cli.codex_home_for_user(1)
    assert (home.stat().st_mode & 0o777) == 0o700


def test_user_id_cannot_escape_the_state_root(state_root):
    """user_id is coerced to int, so no path traversal via a crafted value."""
    with pytest.raises((ValueError, TypeError)):
        cli.codex_home_for_user("../../etc")


def test_environment_drops_the_operator_openai_key(state_root, monkeypatch):
    """An ambient OPENAI_API_KEY would let the CLI do an API-key login and quietly
    bill the operator instead of using the user's plan."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-OPERATOR")
    env = cli._env_for(1)

    assert "OPENAI_API_KEY" not in env
    assert env["CODEX_HOME"] == str(cli.codex_home_for_user(1))


def test_state_root_is_not_under_tmp():
    """The CLI refuses to create its helper binaries under a temp dir, so the
    real default must not live there."""
    assert not str(cli.CODEX_STATE_ROOT).startswith("/tmp")


# --- output parsing ----------------------------------------------------------


def test_parses_url_and_code_from_ansi_coloured_output():
    """The CLI writes colour codes; matching must survive them."""
    line_url = "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m"
    line_code = "   \x1b[94mGY6J-ST7ZS\x1b[0m"

    assert cli._URL_RE.search(cli._strip_ansi(line_url)).group(0) == (
        "https://auth.openai.com/codex/device"
    )
    assert cli._CODE_RE.search(cli._strip_ansi(line_code)).group(1) == "GY6J-ST7ZS"


def test_code_regex_does_not_match_the_url_line():
    """A greedy code match against the URL line would show the user a bogus code."""
    assert cli._CODE_RE.search("https://auth.openai.com/codex/device") is None


# --- credential state --------------------------------------------------------


def test_login_finished_is_false_without_an_access_token(state_root):
    import json

    home = cli.codex_home_for_user(1)
    (home / "auth.json").write_text(json.dumps({"tokens": {"refresh_token": "r"}}))
    assert cli.login_finished(1) is False


def test_login_finished_is_true_with_an_access_token(state_root):
    import json

    home = cli.codex_home_for_user(1)
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "a"}}))
    assert cli.login_finished(1) is True


def test_unreadable_auth_json_reads_as_not_logged_in(state_root):
    home = cli.codex_home_for_user(1)
    (home / "auth.json").write_text("{ not json")
    assert cli.read_auth_json(1) is None
    assert cli.login_finished(1) is False


def test_forget_removes_the_credential(state_root):
    """Disconnect must not leave a copy the next refresh would silently reuse."""
    import json

    home = cli.codex_home_for_user(1)
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "a"}}))
    assert cli.login_finished(1)

    cli.forget(1)

    assert not home.exists()
    assert cli.login_finished(1) is False


def test_forget_is_safe_when_nothing_is_stored(state_root):
    cli.forget(12345)  # must not raise


# --- degradation -------------------------------------------------------------


def test_start_without_the_cli_explains_itself(state_root, monkeypatch):
    """Older images have no codex binary; the message must say so rather than
    surfacing a FileNotFoundError."""
    monkeypatch.setattr(cli, "cli_available", lambda: False)

    with pytest.raises(cli.CodexCliError) as exc:
        asyncio.run(cli.start_device_login(1))

    assert "not installed" in str(exc.value).lower()


def test_refresh_returns_none_without_the_cli(state_root, monkeypatch):
    """get_llm must be able to fail open rather than raise mid-turn."""
    monkeypatch.setattr(cli, "cli_available", lambda: False)
    assert asyncio.run(cli.refresh_via_cli(1)) is None


def test_refresh_returns_none_when_not_logged_in(state_root, monkeypatch):
    monkeypatch.setattr(cli, "cli_available", lambda: True)
    assert asyncio.run(cli.refresh_via_cli(1)) is None


def test_cancel_is_safe_with_no_pending_login(state_root):
    asyncio.run(cli.cancel_device_login(999))  # must not raise
