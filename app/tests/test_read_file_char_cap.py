"""read_file caps what it returns by characters, not just lines.

The 2000-line default let a whole db/schema.rb (~1,470 lines, 73k chars,
~20k tokens) through as one ToolMessage on the first turn of a thread
(crm-4, 2026-09-21). That one message is what compaction then choked on. A
long file now comes back as a first page with a note saying where to
continue, so the agent pages or greps instead of flooding its own context.

Run with: pytest app/tests/test_read_file_char_cap.py -v
"""
import pytest

from app.agents.leonardo.rails_agent import tools


@pytest.fixture
def rails_root(tmp_path, monkeypatch):
    project_root = tmp_path / "app"
    app_dir = project_root / "app"
    root = app_dir / "rails"
    root.mkdir(parents=True)
    monkeypatch.setattr(tools, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(tools, "APP_DIR", app_dir)
    monkeypatch.setattr(tools, "RAILS_ROOT", root)
    return root


def _schema(rails_root, lines=1470, width=48):
    (rails_root / "db").mkdir()
    path = rails_root / "db" / "schema.rb"
    path.write_text("\n".join(f'    t.string "column_{i:04d}"'.ljust(width, "#") for i in range(lines)))
    return path


def test_a_large_file_is_cut_at_the_character_cap(rails_root):
    _schema(rails_root)  # ~73k chars, under the 2000-line limit

    out = tools.read_file.func("db/schema.rb", runtime=None)

    assert len(out) <= tools.READ_FILE_MAX_CHARS + 500  # page + the note
    assert "column_0000" in out
    assert "column_1469" not in out


def test_the_cut_tells_the_agent_how_to_continue(rails_root):
    _schema(rails_root)

    out = tools.read_file.func("db/schema.rb", runtime=None)
    last_shown = int(out.split("\n")[-3].split("\t")[0])  # the line before the note

    assert f"offset={last_shown}" in out
    assert "1470" in out  # total lines, so the agent knows how much is left
    assert "grep_files" in out


def test_the_next_page_picks_up_where_the_note_said(rails_root):
    _schema(rails_root)
    first = tools.read_file.func("db/schema.rb", runtime=None)
    last_shown = int(first.split("\n")[-3].split("\t")[0])

    second = tools.read_file.func("db/schema.rb", runtime=None, offset=last_shown)

    assert second.split("\n")[0].split("\t")[0].strip() == str(last_shown + 1)


def test_a_normal_file_is_returned_whole_with_no_note(rails_root):
    _schema(rails_root, lines=200)

    out = tools.read_file.func("db/schema.rb", runtime=None)

    assert "column_0199" in out
    assert "offset=" not in out
