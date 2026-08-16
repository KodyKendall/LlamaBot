"""A write the running Rails app can't see must say so.

`leo-fotesu`, 2026-08-13 (beginner mode): the agent wrote
`config/initializers/source_admin.rb`, `ls` showed the file, `write_file`
reported success — and the running Rails app never loaded it, because the Rails
container only bind-mounts part of the project (`app/`, `db/`, `spec/`,
`config/routes.rb`, `config/initializers/custom/`, …). The agent then spent the
rest of the turn debugging application code that was never running.

The mount set lives in Leonardo's compose file, not here, so we do not hardcode
it: after creating a NEW file we ask the Rails container whether it can see it.
"""

from unittest.mock import patch

import pytest

from app.agents.leonardo.rails_agent import tools


class _Runtime:
    tool_call_id = "call_1"


@pytest.fixture
def rails_root(tmp_path, monkeypatch):
    root = tmp_path / "rails"
    (root / "app" / "models").mkdir(parents=True)
    (root / "config" / "initializers").mkdir(parents=True)
    monkeypatch.setattr(tools, "RAILS_ROOT", root)
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    return root


def _message(command):
    return command.update["messages"][0].content


def test_a_new_file_the_rails_app_cannot_see_is_flagged(rails_root):
    with patch.object(tools, "rails_app_can_see", return_value=False) as seen:
        result = tools.write_file.func(
            "config/initializers/source_admin.rb", "SOURCE_EDIT_PASSWORD = 'x'\n",
            runtime=_Runtime(),
        )

    seen.assert_called_once()
    message = _message(result)
    assert "not visible" in message.lower(), message
    assert "config/initializers/source_admin.rb" in message
    # The agent needs to know what to do instead, not just that it failed.
    assert "config/initializers/custom" in message, message


def test_a_new_file_in_a_mounted_path_is_not_flagged(rails_root):
    with patch.object(tools, "rails_app_can_see", return_value=True):
        result = tools.write_file.func(
            "app/models/thing.rb", "class Thing; end\n", runtime=_Runtime()
        )

    message = _message(result)
    assert "not visible" not in message.lower(), message
    assert "Updated file" in message


def test_overwriting_an_existing_file_skips_the_check(rails_root):
    """Editing a file the agent already read is not the failure mode, and the
    check costs a docker exec — do not pay it on every write."""
    (rails_root / "app" / "models" / "user.rb").write_text("class User; end\n")

    with patch.object(tools, "rails_app_can_see") as seen:
        tools.write_file.func(
            "app/models/user.rb", "class User; def x; end; end\n", runtime=_Runtime()
        )

    seen.assert_not_called()


def test_the_check_never_breaks_a_write(rails_root):
    """No Docker socket, no Rails container, a timeout — the write still stands."""
    with patch.object(tools, "rails_app_can_see", side_effect=RuntimeError("no docker")):
        result = tools.write_file.func(
            "app/models/thing.rb", "class Thing; end\n", runtime=_Runtime()
        )

    assert "Updated file" in _message(result)
    assert (rails_root / "app" / "models" / "thing.rb").exists()


def test_rails_app_can_see_reads_the_containers_own_filesystem():
    """It must ask the Rails container, not stat the local path (which always
    exists — that is exactly why the bug was invisible)."""
    with patch.object(tools, "rails_api_sh", return_value="VISIBLE\n") as sh:
        assert tools.rails_app_can_see("config/routes.rb") is True

    snippet = sh.call_args.args[0]
    assert "config/routes.rb" in snippet

    with patch.object(tools, "rails_api_sh", return_value="MISSING\n"):
        assert tools.rails_app_can_see("config/initializers/source_admin.rb") is False

    # An unreadable answer is "don't know" — never a false alarm.
    with patch.object(tools, "rails_api_sh", return_value="CREATE-EXEC ERROR: ..."):
        assert tools.rails_app_can_see("app/models/user.rb") is None
