"""Absolute file paths must resolve to the file the agent actually meant.

The system prompt documents the Rails project's absolute root (``/app/app/rails``
in the container), and agents use it. ``guard_against_beginning_slash_argument``
then stripped the leading slash and treated what was left as *relative to
RAILS_ROOT*, so every absolute path was re-rooted a second time:

    /app/app/rails/db/schema.rb  ->  /app/app/rails/app/rails/db/schema.rb   ✗
    /app/spec/requests/x_spec.rb ->  /app/app/rails/app/spec/requests/x_spec.rb ✗
    app/controllers/foo.rb       ->  /app/app/rails/app/controllers/foo.rb   ✓

That last shape — a relative path under ``app/`` — is the only one the guard
could not corrupt, which is why agents reported "read_file works for app/ but
not for spec/, db/ or config/" and fell back to ``bash cat`` (unbounded tool
output, which feeds the summarization loop). 8 friction reports across 4 boxes
on 0.7.0 — the largest cluster.

The layout below mirrors the container exactly:

    PROJECT_ROOT = /app          APP_DIR = /app/app       RAILS_ROOT = /app/app/rails
"""

import pytest

from app.agents.leonardo.rails_agent import tools


@pytest.fixture
def rails_tree(tmp_path, monkeypatch):
    """A fake Rails project laid out like the container's."""
    project_root = tmp_path / "app"
    app_dir = project_root / "app"
    rails_root = app_dir / "rails"

    for rel in (
        "db/schema.rb",
        "config/routes.rb",
        "spec/requests/portal_spec.rb",
        "app/models/production_project_item.rb",
        "app/models/user.rb",
        "app/views/rate_schedule_reports/show.html.erb",
    ):
        target = rails_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n")

    monkeypatch.setattr(tools, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(tools, "APP_DIR", app_dir)
    monkeypatch.setattr(tools, "RAILS_ROOT", rails_root)
    return rails_root


# (input the agent typed, path under RAILS_ROOT it meant)
RESOLVES_TO = [
    # The documented absolute root — the shape in every friction report.
    ("{rails}/db/schema.rb", "db/schema.rb"),
    ("{rails}/app/models/production_project_item.rb", "app/models/production_project_item.rb"),
    ("{rails}/app/views/rate_schedule_reports/show.html.erb",
     "app/views/rate_schedule_reports/show.html.erb"),
    ("{rails}/config/routes.rb", "config/routes.rb"),
    # The container's own root, which agents also reach for.
    ("{project}/spec/requests/portal_spec.rb", "spec/requests/portal_spec.rb"),
    ("{project}/db/schema.rb", "db/schema.rb"),
    ("{project}/config/routes.rb", "config/routes.rb"),
    # /app/<rails app subdir> means the project's app/ dir — the case the old
    # prefix rules were written for. It must keep working.
    ("{project}/models/user.rb", "app/models/user.rb"),
    # Prefix shapes the old heuristics already handled, on relative input.
    ("/rails/app/models/user.rb", "app/models/user.rb"),
    ("rails/config/routes.rb", "config/routes.rb"),
    ("app/app/models/user.rb", "app/models/user.rb"),
    # Plain relative paths — never regress these.
    ("app/models/user.rb", "app/models/user.rb"),
    ("db/schema.rb", "db/schema.rb"),
]


def _expand(raw, rails_root):
    return raw.format(rails=rails_root, project=rails_root.parent.parent)


@pytest.mark.parametrize("raw,expected", RESOLVES_TO)
def test_paths_resolve_to_the_intended_file(rails_tree, raw, expected):
    resolved = tools.resolve_within_rails(_expand(raw, rails_tree))
    assert resolved == (rails_tree / expected).resolve()


@pytest.mark.parametrize("raw,expected", RESOLVES_TO)
def test_read_file_returns_the_contents(rails_tree, raw, expected):
    result = tools.read_file.func(_expand(raw, rails_tree), runtime=None)
    assert "not found" not in result, result
    assert expected in result


def test_new_file_lands_next_to_its_existing_siblings(rails_tree):
    """write_file targets a path that does not exist yet.

    ``/app/spec/models/thing_spec.rb`` has no file to point at, so the parent
    directory decides: ``spec/models`` is real, ``app/spec/models`` is not.
    """
    (rails_tree / "spec" / "models").mkdir(parents=True)
    target = f"{rails_tree.parent.parent}/spec/models/thing_spec.rb"

    assert tools.resolve_within_rails(target) == (
        rails_tree / "spec" / "models" / "thing_spec.rb"
    ).resolve()


def test_relative_traversal_is_still_refused(rails_tree):
    with pytest.raises(tools.PathTraversalError):
        tools.resolve_within_rails("../../etc/passwd")


def test_absolute_path_outside_the_project_is_refused(rails_tree, tmp_path):
    """An absolute path that genuinely escapes must say so.

    Silently re-rooting it under RAILS_ROOT turns a clear refusal into a
    confusing "file not found" for a file the agent can see with its own eyes.
    """
    outside = tmp_path / "secrets.env"
    outside.write_text("SECRET=1")

    with pytest.raises(tools.PathTraversalError):
        tools.resolve_within_rails(str(outside))

    result = tools.read_file.func(str(outside), runtime=None)
    assert "outside the Rails project" in result
