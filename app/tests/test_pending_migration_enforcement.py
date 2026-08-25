"""P0 (2026-08-23): a migration Leo writes but never runs takes the whole app down.

`ActiveRecord::PendingMigrationError` is the highest-occurrence error anywhere in
the fleet — 288 occurrences, 21 customer boxes, 7 days. Rails checks for pending
migrations on EVERY request, so one unrun migration does not break one page, it
breaks every page of the customer's app instantly with a stack trace. The
customer's reading of that is "my app disappeared", and they are right.

Enforced at the WRITE rather than in the prompt: the agent's own write is the
trigger, a prompt rule is something it can talk past (288 occurrences say it
did), and the write tools are shared by every mode — including the raw
StateGraph ones that run no middleware at all.
"""

import pytest

from app.agents.leonardo.rails_agent import tools


class _Runtime:
    tool_call_id = "call_1"


@pytest.fixture
def rails_root(tmp_path, monkeypatch):
    root = tmp_path / "rails"
    (root / "db" / "migrate").mkdir(parents=True)
    (root / "app" / "models").mkdir(parents=True)
    monkeypatch.setattr(tools, "RAILS_ROOT", root)
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    return root


@pytest.fixture
def rails_sh(monkeypatch):
    """Capture what would be run in the Rails container."""
    calls = []
    box = {"output": "== 20260822190000 CreateRosterCrm: migrated (0.0123s) =="}

    def _fake(snippet, workdir=None, timeout_seconds=None):
        calls.append(snippet)
        return box["output"]

    monkeypatch.setattr(tools, "rails_api_sh", _fake)
    return calls, box


MIGRATION = "db/migrate/20260822190000_create_roster_crm.rb"
MIGRATION_BODY = (
    "class CreateRosterCrm < ActiveRecord::Migration[7.1]\n"
    "  def change\n    create_table :roster_crms\n  end\nend\n"
)


def _write(path, content):
    return tools.write_file.func(
        file_path=path, content=content, runtime=_Runtime(),
    ).update["messages"][0].content


def _edit(path, old, new):
    return tools.edit_file.func(
        file_path=path, old_string=old, new_string=new,
        replace_all=False, runtime=_Runtime(),
    ).update["messages"][0].content


class TestPathDetection:
    @pytest.mark.parametrize("path", [
        "db/migrate/20260822190000_create_roster_crm.rb",
        "/rails/db/migrate/20260101000000_add_x.rb",
        "./db/migrate/20260101000000_add_x.rb",
    ])
    def test_migration_paths_are_recognised(self, path):
        assert tools.is_migration_path(path)

    @pytest.mark.parametrize("path", [
        "app/models/roster_crm.rb",
        "db/schema.rb",
        "db/seeds.rb",
        "app/views/db/migrate/thing.html.erb",
        "db/migrate/README.md",
    ])
    def test_everything_else_is_not(self, path):
        assert not tools.is_migration_path(path)


class TestWritingAMigrationRunsIt:
    def test_write_file_runs_db_migrate(self, rails_root, rails_sh):
        calls, _ = rails_sh
        out = _write(MIGRATION, MIGRATION_BODY)

        assert any("db:migrate" in c for c in calls), (
            "a migration was written and never run — every page of the app now 500s"
        )
        assert "migrated" in out

    def test_edit_file_runs_it_too(self, rails_root, rails_sh):
        calls, _ = rails_sh
        (rails_root / MIGRATION).write_text(MIGRATION_BODY)

        _edit(MIGRATION, "create_table :roster_crms", "create_table :rosters")
        assert any("db:migrate" in c for c in calls)

    def test_an_ordinary_file_does_not_trigger_it(self, rails_root, rails_sh):
        calls, _ = rails_sh
        _write("app/models/roster_crm.rb", "class RosterCrm < ApplicationRecord\nend\n")
        assert not any("db:migrate" in c for c in calls)


class TestFailureIsReportedHonestly:
    def test_a_failed_migration_is_not_reported_as_success(self, rails_root, rails_sh):
        _, box = rails_sh
        box["output"] = (
            "rails aborted!\nStandardError: An error has occurred, this and all later "
            "migrations canceled:\n\nPG::UndefinedTable: ERROR:  relation \"users\" does not exist"
        )
        out = _write(MIGRATION, MIGRATION_BODY)

        assert "FAILED" in out
        assert "PG::UndefinedTable" in out

    def test_it_says_the_whole_app_is_down_not_just_the_feature(self, rails_root, rails_sh):
        _, box = rails_sh
        box["output"] = "rails aborted!\nStandardError: boom"
        out = _write(MIGRATION, MIGRATION_BODY)
        assert "EVERY page" in out

    def test_it_forbids_claiming_the_feature_is_ready(self, rails_root, rails_sh):
        _, box = rails_sh
        box["output"] = "rails aborted!\nStandardError: boom"
        out = _write(MIGRATION, MIGRATION_BODY)
        assert "Do NOT tell the user the feature is ready" in out

    def test_it_forbids_writing_a_second_migration_for_the_same_change(self, rails_root, rails_sh):
        """ActiveRecord::DuplicateMigrationNameError, same 7 days, same root cause."""
        _, box = rails_sh
        box["output"] = "rails aborted!\nStandardError: boom"
        out = _write(MIGRATION, MIGRATION_BODY)
        assert "do not write a second migration" in out

    def test_an_unreachable_container_is_reported_not_swallowed(self, rails_root, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("no docker socket")

        monkeypatch.setattr(tools, "rails_api_sh", _boom)
        out = _write(MIGRATION, MIGRATION_BODY)
        assert "FAILED" in out
        assert "no docker socket" in out

    def test_empty_output_counts_as_failure(self, rails_root, rails_sh):
        _, box = rails_sh
        box["output"] = ""
        out = _write(MIGRATION, MIGRATION_BODY)
        assert "FAILED" in out

    def test_the_report_is_bounded(self, rails_root, rails_sh):
        _, box = rails_sh
        box["output"] = "rails aborted!\n" + ("x" * 50_000)
        out = _write(MIGRATION, MIGRATION_BODY)
        assert len(out) < 6000


class TestPromptRule:
    """Every mode that can write a migration must also be told the rule."""

    MODES = [
        ("rails_agent", "RAILS_AGENT_PROMPT"),
        ("rails_beginner_agent", "BEGINNER_AGENT_PROMPT"),
    ]

    @pytest.mark.parametrize("module,const", MODES)
    def test_the_prompt_says_run_it_in_the_same_turn(self, module, const):
        import importlib

        prompts = importlib.import_module(f"app.agents.leonardo.{module}.prompts")
        text = getattr(prompts, const).lower()
        assert "never end a turn with an unrun migration" in text
