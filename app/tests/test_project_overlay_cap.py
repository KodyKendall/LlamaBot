"""LP-12: cap the project overlay so new chats don't start at 84k tokens.

leo-lanupa carried a 191 KB LEONARDO.md + 86 KB MEMORY.md, and both were appended to
every system prompt whole. These tests assert the overlay is budgeted in tokens, the
top of LEONARDO.md survives, and the agent is told where the full file lives.
"""

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    import app.agents.leonardo.project_context as project_context

    importlib.reload(project_context)
    leo_dir = tmp_path / ".leonardo"
    leo_dir.mkdir()
    monkeypatch.setattr(project_context, "LEONARDO_MD_PATH", str(leo_dir / "LEONARDO.md"))
    monkeypatch.setattr(project_context, "MEMORY_MD_PATH", str(leo_dir / "MEMORY.md"))
    project_context._dir = leo_dir
    return project_context


def _big_leonardo_md(sections: int = 400) -> str:
    head = "# Project: Lanupa\n\nTOP-PRIORITY: this app sells surf lessons.\n"
    body = "".join(
        f"\n## Section {i}\n\n" + ("Notes about a feature that shipped long ago. " * 20) + "\n"
        for i in range(sections)
    )
    return head + body


def _big_memory_md(entries: int = 300) -> str:
    return "# Leonardo Memories\n" + "".join(
        f"\n### memory {i} (memory-{i}.md)\n_desc {i}_\n\n" + ("remembered detail " * 40) + "\n"
        for i in range(entries)
    )


def _overlay(prompt: str, base: str) -> str:
    return prompt[len(base):]


class TestOverlayBudget:
    def test_small_overlay_is_untouched(self, ctx):
        (ctx._dir / "LEONARDO.md").write_text("# Small project\n\nJust a note.\n")
        (ctx._dir / "MEMORY.md").write_text("# Leonardo Memories\n\n### a (a.md)\n_a_\n")
        with patch.object(ctx.system_prompt_cache, "get_cached", return_value=None):
            prompt = ctx.build_system_prompt_with_project_context("BASE")
        assert "Just a note." in prompt
        assert "### a (a.md)" in prompt
        assert "Truncated:" not in prompt

    @pytest.mark.parametrize("builder", ["build_system_prompt_with_project_context", "build_beginner_system_prompt"])
    def test_oversized_overlay_is_capped_in_tokens(self, ctx, builder):
        (ctx._dir / "LEONARDO.md").write_text(_big_leonardo_md())
        (ctx._dir / "MEMORY.md").write_text(_big_memory_md())
        with patch.object(ctx.system_prompt_cache, "get_cached", return_value=None):
            prompt = getattr(ctx, builder)("BASE")

        overlay = _overlay(prompt, prompt[: prompt.index("BASE") + len("BASE")])
        # Budget is ~12k; leave slack for headings/notes but nowhere near the raw size.
        assert ctx.count_tokens(overlay) <= ctx.PROJECT_OVERLAY_TOKEN_BUDGET + 200
        assert ctx.count_tokens(_big_leonardo_md()) > ctx.PROJECT_OVERLAY_TOKEN_BUDGET  # premise

    def test_top_of_leonardo_md_survives_and_tail_is_dropped(self, ctx):
        (ctx._dir / "LEONARDO.md").write_text(_big_leonardo_md())
        with patch.object(ctx.system_prompt_cache, "get_cached", return_value=None):
            prompt = ctx.build_system_prompt_with_project_context("BASE")
        assert "TOP-PRIORITY: this app sells surf lessons." in prompt
        assert "## Section 0\n" in prompt
        assert "## Section 399\n" not in prompt

    def test_truncation_note_points_at_the_full_file(self, ctx):
        (ctx._dir / "LEONARDO.md").write_text(_big_leonardo_md())
        (ctx._dir / "MEMORY.md").write_text(_big_memory_md())
        with patch.object(ctx.system_prompt_cache, "get_cached", return_value=None):
            prompt = ctx.build_system_prompt_with_project_context("BASE")
        assert "Truncated: full file at .leonardo/LEONARDO.md" in prompt
        assert "read it with read_leonardo_md" in prompt

    def test_cap_never_rewrites_the_file_on_disk(self, ctx):
        original = _big_leonardo_md()
        (ctx._dir / "LEONARDO.md").write_text(original)
        with patch.object(ctx.system_prompt_cache, "get_cached", return_value=None):
            ctx.build_system_prompt_with_project_context("BASE")
        assert (ctx._dir / "LEONARDO.md").read_text() == original

    def test_single_giant_section_is_still_capped(self, ctx):
        (ctx._dir / "LEONARDO.md").write_text("# One blob\n\n" + ("word " * 60000))
        with patch.object(ctx.system_prompt_cache, "get_cached", return_value=None):
            prompt = ctx.build_system_prompt_with_project_context("BASE")
        assert ctx.count_tokens(prompt) <= ctx.PROJECT_OVERLAY_TOKEN_BUDGET + 200
        assert "Truncated: full file at .leonardo/LEONARDO.md" in prompt


class TestRailsGitNote:
    def test_overlay_tells_the_agent_where_the_git_repo_is(self, ctx):
        with patch.object(ctx.system_prompt_cache, "get_cached", return_value=None):
            prompt = ctx.build_system_prompt_with_project_context("BASE")
        assert "/rails has no .git" in prompt
        assert "/app/leonardo" in prompt


@pytest.fixture
def memory_mod(tmp_path, monkeypatch):
    import app.agents.leonardo.memory as memory

    importlib.reload(memory)
    mem_dir = tmp_path / ".leonardo" / "memory"
    mem_dir.mkdir(parents=True)
    monkeypatch.setattr(memory, "MEMORY_DIR", str(mem_dir))
    monkeypatch.setattr(memory, "MEMORY_MD_PATH", str(tmp_path / ".leonardo" / "MEMORY.md"))
    memory._index = tmp_path / ".leonardo" / "MEMORY.md"
    return memory


class TestLeanMemoryIndex:
    def test_index_lists_metadata_not_bodies(self, memory_mod):
        memory_mod.write_memory_file("prefers-tailwind", "Use Tailwind, not Bootstrap", "user", "SECRET-BODY " * 50)
        index = memory_mod._index.read_text()
        assert "prefers-tailwind" in index
        assert "Use Tailwind, not Bootstrap" in index
        assert "prefers-tailwind.md" in index
        assert "SECRET-BODY" not in index

    def test_dated_name_updates_the_existing_topic_in_place(self, memory_mod):
        first = memory_mod.write_memory_file("deploy-notes", "How deploys work", "project", "v1")
        second = memory_mod.write_memory_file("deploy-notes-2026-10-30", "How deploys work", "project", "v2")
        assert first == second == "deploy-notes.md"
        files = sorted(p.name for p in Path(memory_mod.MEMORY_DIR).glob("*.md"))
        assert files == ["deploy-notes.md"]
        assert "v2" in (Path(memory_mod.MEMORY_DIR) / "deploy-notes.md").read_text()

    @pytest.mark.parametrize("name", ["status", "session-summary-2026-10-30", "progress update", "work-log"])
    def test_status_only_memories_are_refused(self, memory_mod, name):
        with pytest.raises(ValueError, match="status"):
            memory_mod.write_memory_file(name, "what I did today", "project", "Finished the header.")
        assert list(Path(memory_mod.MEMORY_DIR).glob("*.md")) == []

    def test_files_are_owned_by_uid_1000_when_running_as_root(self, memory_mod, monkeypatch):
        calls = []
        monkeypatch.setattr(memory_mod.os, "geteuid", lambda: 0)
        monkeypatch.setattr(memory_mod.os, "chown", lambda p, u, g: calls.append((Path(p).name, u, g)))
        memory_mod.write_memory_file("prefers-tailwind", "Tailwind", "user", "body")
        assert ("prefers-tailwind.md", 1000, 1000) in calls
        assert ("MEMORY.md", 1000, 1000) in calls
