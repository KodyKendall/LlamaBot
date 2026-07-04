"""Tests for the filesystem Agent Skills system (.leonardo/skills/<slug>/SKILL.md).

Covers three layers:
1. The pure filesystem module (app/agents/leonardo/skills.py) — write/list/get/edit/
   delete round-trips, validation caps, and the <available_skills> catalog render.
2. The tools (use_skill + management tools) — the loader returns the SKILL.md body as a
   ToolMessage (progressive disclosure), unknown slug errors, read/write/edit/delete work.
3. The wiring — build_use_skill_tool() bakes the live catalog into its description, the
   RefreshSkillCatalogMiddleware swaps it only when the catalog changes, and a STRUCTURAL
   backstop asserts every memory-tool agent also wires the skill tools (a new agent that
   forgets them fails the suite).

Per CLAUDE.md: assert structure (files, fields, tool-call shape), never exact LLM text.
Filesystem tests chdir into a tmp dir so the real .leonardo/skills is never touched, and
build_workflow() is never called (it clears the asyncio loop — CI landmine), so the agent
wiring test is a static scan.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langgraph.types import Command
from langchain_core.messages import ToolMessage

from app.agents.leonardo import skills as skills_mod


@pytest.fixture
def in_tmp_workspace(tmp_path, monkeypatch):
    """Run with cwd=tmp_path so .leonardo/skills resolves under an isolated dir."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _runtime(tool_call_id="call_test"):
    rt = MagicMock()
    rt.tool_call_id = tool_call_id
    return rt


# ---------------------------------------------------------------------------
# 1. Pure module: skills.py
# ---------------------------------------------------------------------------

def test_sanitize_slug():
    assert skills_mod.sanitize_skill_slug("Rails Migration") == "rails-migration"
    assert skills_mod.sanitize_skill_slug("  Weird__Name!! ") == "weird-name"
    assert skills_mod.sanitize_skill_slug("") == "skill"


def test_write_then_list_and_get(in_tmp_workspace):
    slug = skills_mod.write_skill_file(
        name="Rails Migration",
        description="Do a migration safely.",
        content="# Body\n\nSteps here.",
    )
    assert slug == "rails-migration"

    # File written at the standard path.
    p = Path(".leonardo/skills/rails-migration/SKILL.md")
    assert p.exists()

    listed = skills_mod.list_all_skills()
    assert len(listed) == 1
    s = listed[0]
    assert s["slug"] == "rails-migration"
    assert s["name"] == "Rails Migration"
    assert s["description"] == "Do a migration safely."
    assert "Steps here." in s["body"]


def test_get_skill_body_returns_full_frontmatter_and_body(in_tmp_workspace):
    skills_mod.write_skill_file("Deploy", "Ship it.", "run the deploy")
    body = skills_mod.get_skill_body("deploy")
    assert body is not None
    assert "name: Deploy" in body            # frontmatter preserved
    assert "run the deploy" in body          # body preserved
    assert skills_mod.get_skill_body("does-not-exist") is None


def test_write_is_update_not_duplicate(in_tmp_workspace):
    skills_mod.write_skill_file("Deploy", "v1", "first")
    skills_mod.write_skill_file("Deploy", "v2", "second", slug="deploy")
    listed = skills_mod.list_all_skills()
    assert len(listed) == 1
    assert listed[0]["description"] == "v2"
    assert "second" in listed[0]["body"]


def test_edit_skill_unique_match(in_tmp_workspace):
    skills_mod.write_skill_file("Deploy", "Ship it.", "run the OLD deploy")
    skills_mod.edit_skill_file("deploy", "OLD", "NEW")
    assert "run the NEW deploy" in skills_mod.get_skill_body("deploy")


def test_edit_skill_errors(in_tmp_workspace):
    skills_mod.write_skill_file("Deploy", "Ship it.", "one two two")
    with pytest.raises(ValueError):
        skills_mod.edit_skill_file("deploy", "missing", "x")   # not found
    with pytest.raises(ValueError):
        skills_mod.edit_skill_file("deploy", "two", "x")       # not unique
    with pytest.raises(ValueError):
        skills_mod.edit_skill_file("nope", "one", "x")         # no such skill


def test_delete_skill(in_tmp_workspace):
    skills_mod.write_skill_file("Deploy", "Ship it.", "body")
    assert skills_mod.delete_skill_file("deploy") is True
    assert skills_mod.list_all_skills() == []
    assert skills_mod.delete_skill_file("deploy") is False     # already gone


def test_write_content_cap(in_tmp_workspace, monkeypatch):
    monkeypatch.setattr(skills_mod, "MAX_SKILL_CONTENT_CHARS", 10)
    with pytest.raises(ValueError):
        skills_mod.write_skill_file("Big", "too big", "x" * 11)


def test_write_count_cap(in_tmp_workspace, monkeypatch):
    monkeypatch.setattr(skills_mod, "MAX_SKILLS", 1)
    skills_mod.write_skill_file("One", "d", "b")
    with pytest.raises(ValueError):
        skills_mod.write_skill_file("Two", "d", "b")


def test_render_available_skills_empty_and_populated(in_tmp_workspace):
    assert "No skills" in skills_mod.render_available_skills()
    skills_mod.write_skill_file("Rails Migration", "migrate safely", "body")
    rendered = skills_mod.render_available_skills()
    assert "rails-migration: migrate safely" in rendered


def test_list_ignores_dirs_without_skill_md(in_tmp_workspace):
    Path(".leonardo/skills/empty-dir").mkdir(parents=True)
    skills_mod.write_skill_file("Real", "d", "b")
    listed = skills_mod.list_all_skills()
    assert [s["slug"] for s in listed] == ["real"]


# ---------------------------------------------------------------------------
# 2. Tools
# ---------------------------------------------------------------------------

def test_use_skill_returns_body_as_toolmessage(in_tmp_workspace):
    from app.agents.leonardo.rails_agent.tools import build_use_skill_tool
    skills_mod.write_skill_file("Rails Migration", "migrate", "# How\nRun db:migrate")

    use_skill = build_use_skill_tool()
    result = use_skill.func(slug="rails-migration", runtime=_runtime())

    assert isinstance(result, Command)
    msgs = result.update["messages"]
    assert len(msgs) == 1 and isinstance(msgs[0], ToolMessage)
    assert "Run db:migrate" in msgs[0].content
    assert msgs[0].tool_call_id == "call_test"


def test_use_skill_unknown_slug_errors(in_tmp_workspace):
    from app.agents.leonardo.rails_agent.tools import build_use_skill_tool
    skills_mod.write_skill_file("Deploy", "d", "b")
    result = build_use_skill_tool().func(slug="nope", runtime=_runtime())
    content = result.update["messages"][0].content
    assert "No skill 'nope'" in content
    assert "deploy" in content  # lists available slugs


def test_use_skill_description_lists_catalog(in_tmp_workspace):
    from app.agents.leonardo.rails_agent.tools import build_use_skill_tool
    skills_mod.write_skill_file("Rails Migration", "migrate safely", "b")
    tool = build_use_skill_tool()
    assert tool.name == "use_skill"
    assert "rails-migration: migrate safely" in tool.description


def test_write_read_edit_delete_tools_roundtrip(in_tmp_workspace):
    from app.agents.leonardo.rails_agent.tools import (
        write_skill, read_skill, edit_skill, delete_skill, list_skills,
    )

    # write
    r = write_skill.func(name="Deploy", description="ship", content="old body",
                         runtime=_runtime(), slug=None)
    assert "Skill saved: deploy" in r.update["messages"][0].content

    # read (line-numbered)
    r = read_skill.func(slug="deploy", runtime=_runtime())
    read_out = r.update["messages"][0].content
    assert "SKILL.md" in read_out and "old body" in read_out

    # edit
    r = edit_skill.func(slug="deploy", old_string="old body", new_string="new body",
                        runtime=_runtime())
    assert "Successfully edited" in r.update["messages"][0].content
    assert "new body" in skills_mod.get_skill_body("deploy")

    # list
    r = list_skills.func(runtime=_runtime())
    assert "deploy" in r.update["messages"][0].content

    # delete
    r = delete_skill.func(slug="deploy", runtime=_runtime())
    assert "Skill deleted: deploy" in r.update["messages"][0].content
    assert skills_mod.get_skill_body("deploy") is None


# ---------------------------------------------------------------------------
# 3. Wiring: middleware + structural backstop
# ---------------------------------------------------------------------------

def test_refresh_middleware_swaps_only_on_change(in_tmp_workspace):
    from app.agents.leonardo.agent_factory import RefreshSkillCatalogMiddleware
    from app.agents.leonardo.rails_agent.tools import build_use_skill_tool

    mw = RefreshSkillCatalogMiddleware()

    # Catalog empty at build time.
    baked = build_use_skill_tool()
    other = MagicMock()
    other.name = "read_file"
    tools = [other, baked]

    # No change yet -> no override.
    assert mw._maybe_refresh(tools) is None

    # Author a skill; now the catalog differs -> override with a fresh use_skill.
    skills_mod.write_skill_file("Rails Migration", "migrate safely", "b")
    refreshed = mw._maybe_refresh(tools)
    assert refreshed is not None
    swapped = [t for t in refreshed if getattr(t, "name", None) == "use_skill"][0]
    assert "rails-migration: migrate safely" in swapped.description
    # the non-skill tool is preserved
    assert other in refreshed


def test_refresh_middleware_noop_without_use_skill_tool():
    from app.agents.leonardo.agent_factory import RefreshSkillCatalogMiddleware
    mw = RefreshSkillCatalogMiddleware()
    t = MagicMock()
    t.name = "read_file"
    assert mw._maybe_refresh([t]) is None


# Agents that manage long-term memory should also carry the skill tools (same
# footprint). Static scan of each nodes.py — no graph is built (build_workflow()
# clears the asyncio loop; see the repair-all-agents test's note).
LEONARDO_DIR = Path(__file__).resolve().parents[1] / "agents" / "leonardo"

MEMORY_TOOL_AGENTS = [
    "rails_agent",
    "rails_plan_mode_agent",
    "rails_engineer_plan_mode_agent",
    "rails_ticket_mode_agent",
    "rails_ticket_plan_mode_agent",
    "rails_user_mode_agent",
    "rails_beginner_agent",
    "pyxl_agent",
]


@pytest.mark.parametrize("agent", MEMORY_TOOL_AGENTS)
def test_agent_wires_skill_tools(agent):
    src = (LEONARDO_DIR / agent / "nodes.py").read_text()
    # Only assert for agents that actually have the memory tools (the footprint).
    assert "save_memory" in src, f"{agent} lost its memory tools?"
    for name in ["use_skill", "list_skills", "read_skill", "write_skill",
                 "edit_skill", "delete_skill"]:
        assert name in src, f"{agent}/nodes.py is missing skill tool '{name}'"
    # build_use_skill_tool() must be invoked (per-request/per-turn), not just imported.
    assert re.search(r"build_use_skill_tool\(\)", src), (
        f"{agent}/nodes.py imports but never calls build_use_skill_tool()"
    )


# ---------------------------------------------------------------------------
# 4. /api/skills endpoint — the source the chat's slash menu reads skills from
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_skills_lists_filesystem_skills(async_client, tmp_path, monkeypatch):
    """GET /api/skills returns installed skills as {slug, name, description} — the
    exact shape SlashCommandManager maps into the `/` dropdown."""
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(tmp_path / ".leonardo" / "skills"))
    skills_mod.write_skill_file("Rails Migration", "migrate safely", "# body")

    resp = await async_client.get("/api/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    entry = next((s for s in data if s["slug"] == "rails-migration"), None)
    assert entry is not None, "skill not returned by /api/skills"
    assert set(entry.keys()) == {"slug", "name", "description"}
    assert entry["name"] == "Rails Migration"
    assert entry["description"] == "migrate safely"


@pytest.mark.asyncio
async def test_api_skills_empty_when_none_installed(async_client, tmp_path, monkeypatch):
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(tmp_path / "nope" / "skills"))
    resp = await async_client.get("/api/skills")
    assert resp.status_code == 200
    assert resp.json() == []
