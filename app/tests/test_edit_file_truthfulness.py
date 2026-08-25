"""`edit_file` must never report a success that did not happen.

2026-08-23, 8 friction reports across 6 boxes in 30 days — the most dangerous
item on /admin/agent_friction, because the agent believes the fix landed, tells
the customer it landed, and moves on. Verbatim:

  "edit_file reported 'Successfully replaced string' for a batch edit that never
   actually persisted in the file. The missing code caused a ReferenceError that
   blanked the Sales Funnel page in the user's browser."

  "reported success ('match type: normalized') but the change did not persist."

  "the resulting file was corrupted (a mid-string truncation in an unrelated
   method)."

Two distinct bugs, both reproduced here:

1. The `normalized` path replaced the WHITESPACE-NORMALIZED old_string, which by
   definition is usually not present in the raw file. `str.replace` matched
   nothing, the unchanged content was written back, and the tool said
   "Successfully replaced string (match type: normalized)".
2. Two `fuzzy` rungs accepted difflib's longest common substring when it covered
   >50-70% of old_string. That is not a match; it lands mid-string in unrelated
   code and truncates it.

The concurrent-same-file race from the same report set is covered by
test_file_tool_concurrent_edits.py (fixed 2026-08-12 with a per-path lock).
"""

import pytest

from app.agents.leonardo.rails_agent import tools


class _Runtime:
    tool_call_id = "call_1"


@pytest.fixture
def rails_root(tmp_path, monkeypatch):
    root = tmp_path / "rails"
    (root / "app" / "views").mkdir(parents=True)
    (root / "db" / "migrate").mkdir(parents=True)
    monkeypatch.setattr(tools, "RAILS_ROOT", root)
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    # No Rails container in the test environment.
    monkeypatch.setattr(tools, "migration_followup", lambda _p: "")
    return root


def _edit(path, old, new, replace_all=False):
    return tools.edit_file.func(
        file_path=path, old_string=old, new_string=new,
        replace_all=replace_all, runtime=_Runtime(),
    ).update["messages"][0].content


def _write(path, content):
    return tools.write_file.func(
        file_path=path, content=content, runtime=_Runtime(),
    ).update["messages"][0].content


# ---------------------------------------------------------------------------
# The silent no-op
# ---------------------------------------------------------------------------

class TestNormalizedMatchActuallyEdits:
    def test_a_whitespace_only_mismatch_edits_the_real_bytes(self, rails_root):
        target = rails_root / "app" / "views" / "funnel.html.erb"
        target.write_text('<div>\n  <span   class="a">hi</span>\n</div>\n')

        # Same text, single-spaced — the exact shape that took the normalized path.
        out = _edit(
            "app/views/funnel.html.erb",
            '<span class="a">hi</span>',
            '<span class="b">bye</span>',
        )

        assert "Successfully replaced" in out
        content = target.read_text()
        assert 'class="b">bye' in content, (
            "reported success but the file is unchanged — this is the bug that "
            "blanked a customer's page"
        )
        assert 'class="a">hi' not in content

    def test_indentation_differences_still_match(self, rails_root):
        target = rails_root / "app" / "views" / "a.erb"
        target.write_text("class Foo\n  def bar\n    puts 'hi'\n  end\nend\n")

        _edit("app/views/a.erb", "def bar\n  puts 'hi'\nend", "def bar\n  puts 'bye'\nend")
        assert "bye" in target.read_text()

    def test_an_ambiguous_normalized_match_is_refused(self, rails_root):
        """Two candidate regions: picking one silently edits the wrong method."""
        target = rails_root / "app" / "views" / "b.erb"
        target.write_text("<p>hello</p>\n<p>hello</p>\n")

        out = _edit("app/views/b.erb", "<p>hello</p>\n", "<p>bye</p>\n")
        assert "Could not find" in out or "appears" in out
        assert target.read_text() == "<p>hello</p>\n<p>hello</p>\n"


class TestNoSuccessWithoutAChange:
    def test_an_edit_that_changes_nothing_is_an_error(self, rails_root):
        target = rails_root / "app" / "views" / "c.erb"
        target.write_text("<p>same</p>\n")

        out = _edit("app/views/c.erb", "<p>same</p>", "<p>same</p>")
        assert "Successfully" not in out
        assert "would not change" in out

    def test_a_write_that_does_not_persist_is_reported(self, rails_root, monkeypatch):
        target = rails_root / "app" / "views" / "d.erb"
        target.write_text("original\n")

        # Simulate the disk not holding what we wrote (permissions, a mount, a
        # racing writer) — the tool must not claim success.
        monkeypatch.setattr(
            tools.Path, "write_text", lambda self, *_a, **_k: None, raising=False
        )
        out = _edit("app/views/d.erb", "original", "changed")
        assert "did NOT persist" in out
        assert "Successfully" not in out

    def test_write_file_verifies_too(self, rails_root, monkeypatch):
        monkeypatch.setattr(
            tools.Path, "write_text", lambda self, *_a, **_k: None, raising=False
        )
        out = _write("app/views/e.erb", "hello")
        assert "did NOT persist" in out


# ---------------------------------------------------------------------------
# The corruption
# ---------------------------------------------------------------------------

class TestNoFuzzyCorruption:
    def test_a_string_that_is_not_in_the_file_is_refused(self, rails_root):
        """Previously: difflib's longest common substring was 'close enough',
        and the replacement landed in unrelated code."""
        target = rails_root / "app" / "views" / "f.erb"
        original = (
            "def calculate_total(items)\n"
            "  items.sum(&:price)\n"
            "end\n"
            "\n"
            "def calculate_tax(items)\n"
            "  items.sum(&:tax)\n"
            "end\n"
        )
        target.write_text(original)

        out = _edit(
            "app/views/f.erb",
            "def calculate_total(items)\n  items.map(&:price).reduce(:+)\nend",
            "def calculate_total(items)\n  0\nend",
        )

        assert "Could not find old_string" in out
        assert target.read_text() == original, "the file was modified by a non-match"

    def test_the_failure_message_tells_the_agent_what_to_do(self, rails_root):
        target = rails_root / "app" / "views" / "g.erb"
        target.write_text("<p>a</p>\n")

        out = _edit("app/views/g.erb", "<p>totally different</p>", "<p>b</p>")
        assert "read_file" in out


# ---------------------------------------------------------------------------
# The span mapping itself
# ---------------------------------------------------------------------------

class TestLocateNormalizedSpan:
    def test_it_returns_the_raw_bytes_not_the_normalized_text(self):
        content = 'a\n  <span   class="a">hi</span>\nb\n'
        span = tools.locate_normalized_span(content, '<span class="a">hi</span>')
        assert span is not None
        assert content[span[0]:span[1]] == '<span   class="a">hi</span>'

    def test_crlf_content_maps_back_correctly(self):
        content = "line one\r\nline two\r\n"
        span = tools.locate_normalized_span(content, "line one\nline two")
        assert content[span[0]:span[1]] == "line one\r\nline two"

    def test_blank_line_runs_map_back_correctly(self):
        content = "top\n\n\n\nbottom\n"
        span = tools.locate_normalized_span(content, "top\n\nbottom")
        assert content[span[0]:span[1]] == "top\n\n\n\nbottom"

    def test_no_match_returns_none(self):
        assert tools.locate_normalized_span("abc", "xyz") is None

    def test_ambiguous_returns_none(self):
        assert tools.locate_normalized_span("abc abc", "abc") is None
