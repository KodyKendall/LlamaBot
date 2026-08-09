"""Safety properties of the .env settings surface.

The headline property is a negative one: **nothing here returns the contents of
the .env file** — no values, no key names, not a "configured" bit. The file holds
every provider API key, the database URLs, the SSO login secret and the VS Code
password. The rest of these tests cover the two narrow write paths that do exist:
an allowlisted boolean toggle, and user-defined custom variables that can never
override anything already in the file.
"""

import os

import pytest

from app.services import env_settings_service as svc


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point the service at a throwaway .env and return its path."""
    path = tmp_path / ".env"
    path.write_text(
        "# Instance configuration\n"
        'OPENAI_API_KEY="sk-verysecretvalue1234"\n'
        'VSCODE_PASSWORD="hunter2-shell-access"\n'
        "\n"
        "# Model gates\n"
        "MODEL_SWITCHING_ALLOWED=false\n"
        "DB_URI=postgresql://user:pw@db/llamabot\n"
    )
    monkeypatch.setenv("LEONARDO_ENV_FILE", str(path))
    return path


# --------------------------------------------------------------------------
# The surface discloses nothing
# --------------------------------------------------------------------------

def test_there_is_no_reveal_function(env_file):
    """The reveal path was deleted deliberately. Don't let it come back."""
    assert not hasattr(svc, "reveal")


def test_there_is_no_inventory_function(env_file):
    """No callable returns the file's keys or values to a caller."""
    for gone in ("list_vars", "mask", "classify"):
        assert not hasattr(svc, gone), f"{gone} exposes file contents; it was removed"


def test_toggle_states_expose_only_allowlisted_booleans(env_file, monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    states = svc.toggle_states()

    assert {s["key"] for s in states} == set(svc.TOGGLE_KEYS)
    for entry in states:
        assert isinstance(entry["enabled"], bool)
        # Nothing read out of the file may appear here.
        assert "sk-verysecretvalue1234" not in repr(entry)
        assert "hunter2-shell-access" not in repr(entry)


def test_the_allowlist_is_exactly_the_two_agreed_switches():
    """Widening this is a deliberate act, not an accident."""
    assert set(svc.TOGGLE_KEYS) == {"MODEL_SWITCHING_ALLOWED", "VISION_MODEL_ALLOWED"}


@pytest.mark.parametrize("denied", [
    "PAYWALL_ENABLED",          # revenue gate
    "WS_AUTH_REQUIRED",         # unauthenticated websocket
    "LLAMABOT_COOKIE_SECURE",   # downgrades session cookies
    "LLAMABOT_ENABLE_FAKE_LLM", # silently stubs the model
    "ENABLE_GITHUB_BUTTON",     # operator's call, not the user's
])
def test_dangerous_booleans_are_not_toggleable(denied):
    assert denied not in svc.TOGGLE_KEYS


# --------------------------------------------------------------------------
# The toggle write path
# --------------------------------------------------------------------------

def test_toggle_writes_and_applies_immediately(env_file, monkeypatch):
    monkeypatch.delenv("MODEL_SWITCHING_ALLOWED", raising=False)
    needs_restart = svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)

    assert needs_restart is False
    assert os.environ["MODEL_SWITCHING_ALLOWED"] == "true"
    assert 'MODEL_SWITCHING_ALLOWED="true"' in env_file.read_text()

    from app.agents.leonardo.model_policy import model_switching_allowed
    assert model_switching_allowed() is True


@pytest.mark.parametrize("key", [
    "OPENAI_API_KEY", "DB_URI", "VSCODE_PASSWORD", "SESSION_SECRET",
    "PAYWALL_ENABLED", "WS_AUTH_REQUIRED", "ANYTHING_ELSE",
])
def test_toggle_refuses_every_key_outside_the_allowlist(env_file, key):
    original = env_file.read_text()
    with pytest.raises(svc.EnvValidationError):
        svc.set_toggle(key, True)
    assert env_file.read_text() == original


@pytest.mark.parametrize("payload", [
    "true\nDB_URI=evil", "sk-attacker", {"a": 1}, ["x"], 12345, "'; rm -rf /",
])
def test_toggle_coerces_its_value_so_no_text_reaches_the_file(env_file, payload):
    """The value is derived, not passed through — injection has nothing to grab."""
    svc.set_toggle("MODEL_SWITCHING_ALLOWED", payload)
    text = env_file.read_text()

    assert 'MODEL_SWITCHING_ALLOWED="true"' in text or 'MODEL_SWITCHING_ALLOWED="false"' in text
    assert "evil" not in text
    assert "sk-attacker" not in text
    assert "rm -rf" not in text


def test_toggle_preserves_comments_and_ordering(env_file):
    svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)
    lines = env_file.read_text().splitlines()

    assert "# Instance configuration" in lines
    assert "# Model gates" in lines
    assert sum(1 for line in lines if line.startswith("MODEL_SWITCHING_ALLOWED")) == 1
    assert 'OPENAI_API_KEY="sk-verysecretvalue1234"' in lines  # untouched


# --------------------------------------------------------------------------
# Reserved names — custom variables can never take over a platform key
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "STRIPE_SECRET", "MY_PASSWORD",
    "SOME_TOKEN", "VSCODE_PASSWORD", "DB_URI", "DATABASE_URL", "AWS_KEY",
    "SESSION_SECRET", "RAILS_MASTER_KEY", "LLAMABOT_VERSION", "POSTGRES_PASSWORD",
    "MODEL_SWITCHING_ALLOWED", "PAYWALL_ENABLED", "SCHEDULER_TOKEN",
    "CHATGPT_CREDENTIAL_KEY", "LLAMAPRESS_AI_LOGIN_SECRET", "PATH", "REDIS_URL",
])
def test_reserved_names_are_rejected(env_file, name):
    assert svc.is_reserved(name) is True


@pytest.mark.parametrize("name", ["MY_SERVICE_URL", "REPORT_EMAIL", "FEATURE_X", "ACME_ENDPOINT"])
def test_ordinary_names_are_allowed(env_file, name):
    assert svc.is_reserved(name) is False


def test_a_key_present_only_in_this_file_is_still_reserved(env_file):
    """Otherwise the custom var would render, be dropped as a collision, and
    silently do nothing."""
    assert svc.is_reserved("OPENAI_API_KEY") is True


def test_the_rejection_message_is_the_same_for_every_reason():
    """A per-case message would let someone probe what this box has configured."""
    assert "reserved or already in use" in svc.RESERVED_MESSAGE
    assert "OPENAI" not in svc.RESERVED_MESSAGE
    assert ".env" not in svc.RESERVED_MESSAGE


# --------------------------------------------------------------------------
# Custom variables never override
# --------------------------------------------------------------------------

def test_custom_var_never_overrides_existing_key(env_file):
    """Compose takes the LAST duplicate and the block is at the end of the file,
    so an unfiltered render would silently replace the operator's value."""
    svc.sync_managed_block({"OPENAI_API_KEY": "sk-attacker", "MY_APP_URL": "https://x"})

    text = env_file.read_text()
    assert "sk-attacker" not in text
    assert "sk-verysecretvalue1234" in text
    assert 'MY_APP_URL="https://x"' in text


def test_shadowed_custom_var_is_dropped_when_key_appears_later(env_file):
    """The render-time filter catches a collision created AFTER the var was saved."""
    svc.sync_managed_block({"LATER_KEY": "custom-value"})
    assert "custom-value" in env_file.read_text()

    env_file.write_text(env_file.read_text().replace(
        "# Model gates", 'LATER_KEY="operator-value"\n# Model gates'))
    svc.sync_managed_block({"LATER_KEY": "custom-value"})

    text = env_file.read_text()
    assert "custom-value" not in text
    assert "operator-value" in text


def test_managed_block_is_rewritten_not_duplicated(env_file):
    svc.sync_managed_block({"A_VAR": "1"})
    svc.sync_managed_block({"B_VAR": "2"})
    text = env_file.read_text()

    assert text.count(svc.MANAGED_BEGIN) == 1
    assert text.count(svc.MANAGED_END) == 1
    assert "A_VAR" not in text
    assert 'B_VAR="2"' in text


def test_toggle_preserves_the_managed_block(env_file):
    svc.sync_managed_block({"A_VAR": "1"})
    svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)
    text = env_file.read_text()

    assert 'A_VAR="1"' in text
    assert text.count(svc.MANAGED_BEGIN) == 1


# --------------------------------------------------------------------------
# Injection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["a\nDB_URI=evil", "a\r\nDB_URI=evil", "a\rb"])
def test_value_with_newline_is_refused(bad):
    """A newline would terminate the assignment and inject a SECOND variable."""
    with pytest.raises(svc.EnvValidationError, match="line breaks"):
        svc.validate_value(bad)


def test_null_byte_is_refused():
    with pytest.raises(svc.EnvValidationError, match="null bytes"):
        svc.validate_value("a\x00b")


@pytest.mark.parametrize("bad", ["lowercase", "1LEADING", "HAS-DASH", "HAS SPACE", "", "  "])
def test_invalid_names_are_refused(bad):
    with pytest.raises(svc.EnvValidationError):
        svc.validate_name(bad)


def test_values_with_special_characters_round_trip(env_file):
    """Quoting has to survive characters that would otherwise break the file."""
    tricky = 'hello # world "quoted" \\ back'
    svc.sync_managed_block({"NOTE_TEXT": tricky})

    rendered = [line for line in env_file.read_text().splitlines()
                if line.startswith("NOTE_TEXT")][0]
    assert svc.parse_line(rendered) == ("NOTE_TEXT", tricky)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    """Redirect backups to a throwaway directory."""
    target = tmp_path / "backups"
    monkeypatch.setattr(svc, "_BACKUP_DIR", target)
    return target


def test_write_makes_a_backup(env_file, backup_dir):
    original = env_file.read_text()
    svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)
    backups = list(backup_dir.glob(".env.bak-*"))

    assert len(backups) == 1
    assert backups[0].read_text() == original


def test_backups_never_land_beside_the_env_file(env_file, backup_dir):
    """Leonardo mounts the project into the code-server container, so a backup
    written next to .env is readable from the customer's editor terminal. The
    2026-08-09 fleet audit found the raw .env exposed that way on 96/97 boxes."""
    svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)

    assert list(env_file.parent.glob("*.bak*")) == []
    assert list(backup_dir.glob(".env.bak-*"))


def test_backups_are_not_world_readable(env_file, backup_dir):
    svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)
    backup = next(iter(backup_dir.glob(".env.bak-*")))

    assert oct(backup.stat().st_mode)[-3:] == "600"


def test_backups_are_pruned(env_file, backup_dir):
    for i in range(9):
        svc.set_toggle("MODEL_SWITCHING_ALLOWED", i % 2 == 0)
    assert len(list(backup_dir.glob(".env.bak-*"))) <= 5


def test_write_preserves_the_original_owner(env_file, monkeypatch):
    """The container runs as root; the host .env belongs to uid 1000.

    A plain rename would hand the host a root-owned .env that the operator and
    bin/update's set_env_var can no longer write. That failure is silent until
    someone tries to edit the file, which is exactly how the root-owned
    checkpoint Restore bug went unnoticed.
    """
    chowned = {}

    def fake_chown(path, uid, gid):
        chowned[str(path)] = (uid, gid)

    monkeypatch.setattr(os, "chown", fake_chown)
    monkeypatch.setattr(os, "getuid", lambda: 0)          # pretend to be root
    monkeypatch.setattr(env_file.__class__, "stat",
                        lambda self: type("S", (), {"st_uid": 1000, "st_gid": 1000,
                                                    "st_mode": 0o100664})())

    svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)

    assert chowned, "the replacement file was never chowned back to the original owner"
    assert set(chowned.values()) == {(1000, 1000)}


def test_chown_failure_does_not_lose_the_edit(env_file, monkeypatch):
    """A failed chown is a warning, not a lost write."""
    def boom(*a, **kw):
        raise OSError("operation not permitted")

    monkeypatch.setattr(os, "chown", boom)
    monkeypatch.setattr(os, "getuid", lambda: 0)

    svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)
    assert 'MODEL_SWITCHING_ALLOWED="true"' in env_file.read_text()


def test_failed_write_leaves_the_original_intact(env_file, monkeypatch):
    """A crash mid-write must not truncate .env — the stack wouldn't boot."""
    original = env_file.read_text()

    def boom(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    with pytest.raises(svc.EnvValidationError):
        svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)

    assert env_file.read_text() == original
    assert not list(env_file.parent.glob(".env.tmp-*"))


# --------------------------------------------------------------------------
# Parsing and degradation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "line,expected",
    [
        ("A=1", ("A", "1")),
        ('A="1"', ("A", "1")),
        ("A='1'", ("A", "1")),
        ("export A=1", ("A", "1")),
        ("  A = 1  ", ("A", "1")),
        ("A=", ("A", "")),
        ("# A=1", None),
        ("", None),
        ("not an assignment", None),
        ("lower=1", None),
    ],
)
def test_parse_line(line, expected):
    assert svc.parse_line(line) == expected


def test_missing_env_file_degrades_to_read_only(tmp_path, monkeypatch):
    """No writable file is a supported state, not a traceback."""
    monkeypatch.setattr(svc, "_candidate_paths", lambda: [tmp_path / "nope.env"])

    assert svc.env_file_path() is None
    assert svc.is_writable() is False
    assert svc.base_keys() == set()
    with pytest.raises(svc.EnvValidationError, match="No writable .env"):
        svc.set_toggle("MODEL_SWITCHING_ALLOWED", True)
