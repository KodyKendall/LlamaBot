"""
Tests for Engineer Plan Mode (0.5.1p).

Covers two shipped changes:
1. Engineer Mode's prompt gained a softened, user-facing "How You Talk" section
   (7th-grade reading level, gloss tech words, scale up to the user).
2. A new `rails_engineer_plan_mode_agent` — the engineering counterpart to Beginner
   Plan Mode: same one-question-at-a-time plan-first mechanism, but retains Engineer
   Mode's full depth (its playbook is composed into the prompt).

These are structural assertions only — we never assert exact LLM output, and we never
call build_workflow() in a unit test (it clears the asyncio event loop; see memory
`ci_build_workflow_event_loop`). We assert registration, importability, prompt
composition, and tool wiring instead.
"""
import json
from pathlib import Path

# app/langgraph.json is the runtime-authoritative graph registry (request_handler walks
# up from app/websocket/ and finds this one first).
LANGGRAPH_JSON = Path(__file__).resolve().parents[1] / "langgraph.json"

AGENT_NAME = "rails_engineer_plan_mode_agent"


class TestEngineerModeVoice:
    """Item 1: the softened 7th-grade voice section is present in Engineer Mode."""

    def test_engineer_prompt_has_how_you_talk_section(self):
        from app.agents.leonardo.rails_agent.prompts import RAILS_AGENT_PROMPT

        assert "How You Talk" in RAILS_AGENT_PROMPT
        assert "7th grader" in RAILS_AGENT_PROMPT
        # Scale-up rule: advanced users get matched, not dumbed down.
        assert "scale up" in RAILS_AGENT_PROMPT.lower()

    def test_engineer_prompt_keeps_engineering_depth(self):
        """The voice change must not have stripped the engineering playbook."""
        from app.agents.leonardo.rails_agent.prompts import RAILS_AGENT_PROMPT

        assert "TURBO FORMS & STREAMS" in RAILS_AGENT_PROMPT
        assert "Sub-Agents" in RAILS_AGENT_PROMPT


class TestEngineerPlanRegistration:
    """The new agent is registered so it can actually route at runtime."""

    def test_agent_in_langgraph_json(self):
        graphs = json.loads(LANGGRAPH_JSON.read_text())["graphs"]
        assert AGENT_NAME in graphs, f"{AGENT_NAME} must be registered in {LANGGRAPH_JSON}"
        assert graphs[AGENT_NAME].endswith(
            "rails_engineer_plan_mode_agent/nodes.py:build_workflow"
        )

    def test_module_importable_with_build_workflow(self):
        # Import only — do NOT call build_workflow() (clears the asyncio loop).
        from app.agents.leonardo.rails_engineer_plan_mode_agent import nodes

        assert hasattr(nodes, "build_workflow")
        assert callable(nodes.build_workflow)


class TestEngineerPlanComposition:
    """The prompt is plan-first AND carries Engineer Mode's depth + voice (DRY compose)."""

    def test_prompt_composes_plan_workflow_plus_engineer_playbook(self):
        from app.agents.leonardo.rails_engineer_plan_mode_agent.prompts import (
            ENGINEER_PLAN_PROMPT,
        )
        from app.agents.leonardo.rails_agent.prompts import RAILS_AGENT_PROMPT

        # Plan-first preamble.
        assert "Engineer Plan Mode" in ENGINEER_PLAN_PROMPT
        assert "6-PHASE WORKFLOW" in ENGINEER_PLAN_PROMPT
        assert "Batch your questions" in ENGINEER_PLAN_PROMPT
        # Full engineer playbook is composed in (single source of truth).
        assert RAILS_AGENT_PROMPT in ENGINEER_PLAN_PROMPT
        # Therefore the softened voice flows through automatically.
        assert "How You Talk" in ENGINEER_PLAN_PROMPT

    def test_plan_interaction_tools_are_wired(self):
        from app.agents.leonardo.rails_engineer_plan_mode_agent.nodes import default_tools

        tool_names = [t.name for t in default_tools]
        # The plan-first mechanism (reused verbatim from plan mode).
        assert "ask_user_question" in tool_names
        assert "ask_user_uiux_question" in tool_names
        # Engineering depth: delegation + build tools present.
        assert "delegate_task" in tool_names
        assert "bash_command" in tool_names
