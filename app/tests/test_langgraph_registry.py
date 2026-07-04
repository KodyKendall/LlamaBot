"""Tests for the layered langgraph.json registry (platform base + client overlay).

The agent-graph registry is split by *ownership* into two files:

  * ``langgraph.json``        — the PLATFORM base. Ships in the image, tracks
                                upstream wholesale (safe to clobber on sync).
  * ``langgraph.local.json``  — the CLIENT overlay. Never synced, never clobbered.
                                Holds downstream/client-registered graphs and is
                                the write target for the AI-builder's edit tool.
    (plus optional ``langgraph.d/*.json`` drop-ins, merged in sorted order.)

``load_graphs()`` deep-merges the ``graphs`` maps with the overlay winning on key
collisions, so a client can register new agents — or shadow a platform one — without
touching the base. These tests pin that merge contract; the runtime graph resolver,
the /agents endpoint, and the custom-agent-mode validator all read through it.

Run with: pytest app/tests/test_langgraph_registry.py -v
"""
import json

import pytest

from app.lib.langgraph_registry import (
    load_graphs,
    load_registry,
    local_overlay_path,
    overlay_paths_for,
)


def _write_base(tmp_path, graphs, **extra):
    p = tmp_path / "langgraph.json"
    p.write_text(json.dumps({"dependencies": ["."], "graphs": graphs, **extra}))
    return p


def _write_overlay(tmp_path, payload, name="langgraph.local.json"):
    p = tmp_path / name
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


def test_base_only_no_overlay(tmp_path):
    # Stock instance: no overlay file → graphs are exactly the base.
    base = _write_base(tmp_path, {"rails_agent": "./agents/leonardo/rails_agent/nodes.py:build_workflow"})
    assert load_graphs(base) == {
        "rails_agent": "./agents/leonardo/rails_agent/nodes.py:build_workflow"
    }


def test_overlay_adds_client_graphs(tmp_path):
    base = _write_base(tmp_path, {"rails_agent": "./agents/leonardo/rails_agent/nodes.py:build_workflow"})
    _write_overlay(tmp_path, {"graphs": {"leo": "./user_agents/leo/nodes.py:build_workflow"}})
    merged = load_graphs(base)
    assert merged == {
        "rails_agent": "./agents/leonardo/rails_agent/nodes.py:build_workflow",
        "leo": "./user_agents/leo/nodes.py:build_workflow",
    }


def test_overlay_wins_on_key_collision(tmp_path):
    # A client may deliberately shadow a platform graph — overlay wins.
    base = _write_base(tmp_path, {"rails_agent": "./agents/leonardo/rails_agent/nodes.py:build_workflow"})
    _write_overlay(tmp_path, {"graphs": {"rails_agent": "./user_agents/custom_rails/nodes.py:build_workflow"}})
    assert load_graphs(base)["rails_agent"] == "./user_agents/custom_rails/nodes.py:build_workflow"


def test_object_format_entry_preserved(tmp_path):
    # The per-agent object format (recursion_limit etc.) must survive the merge intact.
    base = _write_base(tmp_path, {})
    _write_overlay(tmp_path, {"graphs": {"leo": {
        "workflow": "./user_agents/leo/nodes.py:build_workflow",
        "recursion_limit": 200,
    }}})
    assert load_graphs(base)["leo"] == {
        "workflow": "./user_agents/leo/nodes.py:build_workflow",
        "recursion_limit": 200,
    }


def test_missing_overlay_is_not_an_error(tmp_path):
    base = _write_base(tmp_path, {"rails_agent": "x:build_workflow"})
    # No overlay written.
    assert overlay_paths_for(base) == []
    assert "rails_agent" in load_graphs(base)


def test_malformed_overlay_falls_back_to_base(tmp_path):
    # A broken overlay must never take down the whole registry — fail open to base.
    base = _write_base(tmp_path, {"rails_agent": "x:build_workflow"})
    _write_overlay(tmp_path, "{ this is not json ")
    assert load_graphs(base) == {"rails_agent": "x:build_workflow"}


def test_overlay_without_graphs_key_is_ignored(tmp_path):
    base = _write_base(tmp_path, {"rails_agent": "x:build_workflow"})
    _write_overlay(tmp_path, {"something_else": 1})
    assert load_graphs(base) == {"rails_agent": "x:build_workflow"}


def test_dropin_dir_merged_in_sorted_order(tmp_path):
    base = _write_base(tmp_path, {"rails_agent": "x:build_workflow"})
    d = tmp_path / "langgraph.d"
    d.mkdir()
    (d / "10-alpha.json").write_text(json.dumps({"graphs": {"alpha": "a:build_workflow", "shared": "a:build_workflow"}}))
    (d / "20-beta.json").write_text(json.dumps({"graphs": {"beta": "b:build_workflow", "shared": "b:build_workflow"}}))
    merged = load_graphs(base)
    assert merged["alpha"] == "a:build_workflow"
    assert merged["beta"] == "b:build_workflow"
    # Later drop-in (20-beta) wins over earlier (10-alpha) on collision.
    assert merged["shared"] == "b:build_workflow"


def test_local_overlay_and_dropins_both_apply(tmp_path):
    base = _write_base(tmp_path, {"rails_agent": "x:build_workflow"})
    _write_overlay(tmp_path, {"graphs": {"leo": "leo:build_workflow"}})
    d = tmp_path / "langgraph.d"
    d.mkdir()
    (d / "01-extra.json").write_text(json.dumps({"graphs": {"extra": "extra:build_workflow"}}))
    merged = load_graphs(base)
    assert {"rails_agent", "leo", "extra"} <= set(merged)


def test_load_registry_preserves_base_top_level_keys(tmp_path):
    # dependencies / env are platform concerns — only `graphs` is merged from the overlay.
    base = _write_base(tmp_path, {"rails_agent": "x:build_workflow"}, env=".env")
    _write_overlay(tmp_path, {"graphs": {"leo": "leo:build_workflow"}})
    reg = load_registry(base)
    assert reg["dependencies"] == ["."]
    assert reg["env"] == ".env"
    assert set(reg["graphs"]) == {"rails_agent", "leo"}


def test_local_overlay_path_is_sibling_of_base(tmp_path):
    base = _write_base(tmp_path, {})
    assert local_overlay_path(base) == tmp_path / "langgraph.local.json"
