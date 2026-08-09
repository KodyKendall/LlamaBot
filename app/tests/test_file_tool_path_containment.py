"""File tools must stay inside the customer's Rails project.

``guard_against_beginning_slash_argument`` only ever rewrote path *prefixes* — it
never looked at ``..``. That left ``read_file("../../leonardo/.env")`` resolving
to the instance's real ``.env``: every provider API key, the database URLs and
the VS Code password, readable by asking the agent for a file.
``bash_command`` refuses the literal string ``.env``, but the file tools had no
equivalent check, so this was the way around it.
"""

import pytest

from app.agents.leonardo.rails_agent import tools


ESCAPES = [
    "../../leonardo/.env",
    "/rails/../../leonardo/.env",
    "../../../app/leonardo/.env",
    "../../.env",
    "app/../../../etc/passwd",
    "..",
    "../",
    "subdir/../../../../root/.ssh/id_rsa",
]

STAYS_INSIDE = [
    "app/views/home/index.html.erb",
    "/app/models/user.rb",
    "rails/config/routes.rb",
    "app/app/helpers/thing.rb",
    "app/views/../models/user.rb",  # normalizes back inside — legitimate
    "",
]


@pytest.mark.parametrize("path", ESCAPES)
def test_escaping_paths_are_refused(path):
    with pytest.raises(tools.PathTraversalError):
        tools.resolve_within_rails(path)


@pytest.mark.parametrize("path", STAYS_INSIDE)
def test_legitimate_paths_are_allowed(path):
    resolved = tools.resolve_within_rails(path)
    root = tools.RAILS_ROOT.resolve()
    assert resolved == root or resolved.is_relative_to(root)


def test_the_env_file_is_not_reachable():
    """The specific file this fix exists for."""
    for path in ("../../leonardo/.env", "../../../app/leonardo/.env"):
        with pytest.raises(tools.PathTraversalError):
            tools.resolve_within_rails(path)


def test_read_file_returns_an_error_instead_of_raising():
    """A refusal must be correctable by the agent, not fatal to the turn."""
    result = tools.read_file.func("../../leonardo/.env", runtime=None)
    assert isinstance(result, str)
    assert "outside the Rails project" in result


def test_ls_returns_an_error_instead_of_raising():
    result = tools.ls.func("../../leonardo")
    assert isinstance(result, str)
    assert "outside the Rails project" in result


def test_symlink_out_of_the_project_is_refused(tmp_path, monkeypatch):
    """Resolution happens before the check, so a symlink can't smuggle a path out."""
    root = tmp_path / "rails"
    root.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / ".env").write_text("SECRET=1")
    (root / "link").symlink_to(outside)

    monkeypatch.setattr(tools, "RAILS_ROOT", root)
    with pytest.raises(tools.PathTraversalError):
        tools.resolve_within_rails("link/.env")
