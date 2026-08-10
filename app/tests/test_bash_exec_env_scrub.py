"""Shell execs must not inherit the instance's secrets.

The Rails container is started with ``env_file: .env``, so its environment holds
every LLM provider key, the VS Code password and the SSO login secret — and a
docker exec inherits all of it. The old defence was a substring blocklist
(``[".env", "ENV["]``) on the command text, which caught exactly one spelling:
``printenv``, ``env``, ``export -p``, ``echo $OPENAI_API_KEY``,
``ruby -e 'p ENV'`` and any base64 of those all passed it.

So the command text is no longer what protects us — the environment handed to
the exec is scrubbed instead. These tests pin that, including the property that
matters most: a secret nobody has thought of yet is scrubbed by default.
"""

import pytest

from app.agents.leonardo.rails_agent import tools


@pytest.fixture(autouse=True)
def clear_cache():
    tools._container_env_names_cache.clear()
    yield
    tools._container_env_names_cache.clear()


@pytest.fixture
def container_env(monkeypatch):
    """Pretend the container defines this set of variable names."""
    def _set(names):
        monkeypatch.setattr(tools, "_container_env_names", lambda _c: list(names))
    return _set


def _scrubbed(entries):
    """Names blanked by the exec env (``NAME=``)."""
    return {e.split("=", 1)[0] for e in entries if e.endswith("=")}


def _values(entries):
    return {e.split("=", 1)[0]: e.split("=", 1)[1] for e in entries}


SECRETS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "GMI_DEEPSEEK_API_KEY", "FIREWORKS_DEEPSEEK_API_KEY", "GOOGLE_API_KEY",
    "ALIBABA_API_KEY", "META_API_KEY", "BEDROCK_API_KEY", "TAVILY_API_KEY",
    "VSCODE_PASSWORD", "LLAMAPRESS_AI_LOGIN_SECRET", "AUTH_DB_URI",
    "SCHEDULER_TOKEN", "WS_SECRET_KEY", "SESSION_SECRET",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_every_known_secret_is_blanked(container_env, secret):
    container_env([secret, "PATH", "RAILS_ENV"])
    entries = tools.build_exec_env("c", ["RUBYOPT=-W0"])

    assert secret in _scrubbed(entries)
    assert _values(entries)[secret] == ""


def test_an_unknown_future_secret_is_blanked_by_default(container_env):
    """The allowlist direction: this is the property a blocklist cannot give."""
    container_env(["BRAND_NEW_PROVIDER_KEY", "SOME_FUTURE_TOKEN", "PATH"])
    scrubbed = _scrubbed(tools.build_exec_env("c", []))

    assert "BRAND_NEW_PROVIDER_KEY" in scrubbed
    assert "SOME_FUTURE_TOKEN" in scrubbed


@pytest.mark.parametrize("needed", [
    "PATH", "HOME", "RAILS_ENV", "DATABASE_URL", "DB_URI", "REDIS_URL",
    "SECRET_KEY_BASE", "BUNDLE_APP_CONFIG", "GEM_HOME", "AWS_BUCKET",
    "BOOTSNAP_CACHE_DIR", "LLAMABOT_API_URL",
])
def test_variables_rails_needs_are_not_blanked(container_env, needed):
    """Scrubbing these would break db:migrate, bundler and ActiveStorage."""
    container_env([needed])
    assert needed not in _scrubbed(tools.build_exec_env("c", []))


def test_explicit_extras_survive(container_env):
    container_env(["OPENAI_API_KEY"])
    entries = tools.build_exec_env("c", ["RUBYOPT=-W0"])

    assert "RUBYOPT=-W0" in entries
    assert _values(entries)["OPENAI_API_KEY"] == ""


def test_an_extra_is_never_overridden_by_the_scrub(container_env):
    """A caller that deliberately sets a value must win over the blanket blank."""
    container_env(["SOME_TOKEN"])
    entries = tools.build_exec_env("c", ["SOME_TOKEN=deliberate"])

    assert _values(entries)["SOME_TOKEN"] == "deliberate"
    assert entries.count("SOME_TOKEN=") == 0


def test_scrub_still_happens_when_the_container_cannot_be_read(monkeypatch):
    """A Docker API hiccup must not mean "run it with full secrets"."""
    monkeypatch.setattr(tools, "_container_env_names", lambda _c: [])
    scrubbed = _scrubbed(tools.build_exec_env("c", []))

    for secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "VSCODE_PASSWORD"):
        assert secret in scrubbed


def test_enumeration_failure_is_swallowed(monkeypatch):
    """_container_env_names must degrade, not raise, or every command dies."""
    def boom(*a, **kw):
        raise OSError("docker.sock unavailable")

    monkeypatch.setattr(tools.subprocess, "run", boom)
    assert tools._container_env_names("c") == []


def test_enumeration_is_cached(monkeypatch):
    calls = {"n": 0}

    class Result:
        returncode = 0
        stdout = '{"Config": {"Env": ["FOO=bar", "PATH=/usr/bin"]}}'

    def counting_run(*a, **kw):
        calls["n"] += 1
        return Result()

    monkeypatch.setattr(tools.subprocess, "run", counting_run)
    first = tools._container_env_names("c")
    second = tools._container_env_names("c")

    assert first == second == ["FOO", "PATH"]
    assert calls["n"] == 1, "container env should be read once, not per command"


def test_the_allowlist_holds_no_llm_provider_keys():
    """A provider key on the allowlist would silently undo the whole fix."""
    for name in tools._EXEC_ENV_ALLOWLIST:
        assert "API_KEY" not in name or name in ("AWS_KEY",), name
