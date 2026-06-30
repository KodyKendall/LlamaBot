"""
Tests for Ticket Plan Mode.

Ticket Plan Mode is selected when Plan Mode is toggled ON while the user is in Ticket
Mode. It grounds the user-feedback story by asking clarifying questions (one at a time,
with live visual options for look-and-feel decisions) BEFORE running the normal Ticket
Mode flow: delegated deep research -> write the ticket -> offer to auto-implement it.

Structural assertions only — we never assert exact LLM output, and we never call
build_workflow() in a unit test (it clears the asyncio event loop; see memory
`ci_build_workflow_event_loop`). We assert registration, importability, prompt
composition (DRY: the full Ticket Mode playbook is reused verbatim), tool wiring, and
the frontend routing that selects this agent for ticket+plan.
"""
import json
from pathlib import Path

# app/langgraph.json is the runtime-authoritative graph registry (request_handler walks
# up from app/websocket/ and finds this one first).
LANGGRAPH_JSON = Path(__file__).resolve().parents[1] / "langgraph.json"
FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "chat"

AGENT_NAME = "rails_ticket_plan_mode_agent"


class TestTicketPlanRegistration:
    """The new agent is registered so it can actually route at runtime."""

    def test_agent_in_langgraph_json(self):
        graphs = json.loads(LANGGRAPH_JSON.read_text())["graphs"]
        assert AGENT_NAME in graphs, f"{AGENT_NAME} must be registered in {LANGGRAPH_JSON}"
        assert graphs[AGENT_NAME].endswith(
            "rails_ticket_plan_mode_agent/nodes.py:build_workflow"
        )

    def test_module_importable_with_build_workflow(self):
        # Import only — do NOT call build_workflow() (clears the asyncio loop).
        from app.agents.leonardo.rails_ticket_plan_mode_agent import nodes

        assert hasattr(nodes, "build_workflow")
        assert callable(nodes.build_workflow)


class TestTicketPlanComposition:
    """Plan-first preamble + the full Ticket Mode playbook (DRY compose)."""

    def test_prompt_composes_clarify_preamble_plus_ticket_playbook(self):
        from app.agents.leonardo.rails_ticket_plan_mode_agent.prompts import (
            TICKET_PLAN_MODE_AGENT_PROMPT,
        )
        from app.agents.leonardo.rails_ticket_mode_agent.prompts import (
            TICKET_MODE_AGENT_PROMPT,
        )

        # Plan-first clarification preamble.
        assert "Ticket Plan Mode" in TICKET_PLAN_MODE_AGENT_PROMPT
        assert "PHASE 0" in TICKET_PLAN_MODE_AGENT_PROMPT
        assert "ask_user_question" in TICKET_PLAN_MODE_AGENT_PROMPT
        assert "ui_related" in TICKET_PLAN_MODE_AGENT_PROMPT
        # Full Ticket Mode playbook is composed in verbatim (single source of truth):
        # the research -> ticket -> offer flow is inherited, not re-described.
        assert TICKET_MODE_AGENT_PROMPT in TICKET_PLAN_MODE_AGENT_PROMPT

    def test_interaction_and_ticket_tools_are_wired(self):
        from app.agents.leonardo.rails_ticket_plan_mode_agent.nodes import default_tools

        tool_names = [t.name for t in default_tools]
        # Plan-first grounding mechanism (reused verbatim from plan mode).
        assert "ask_user_question" in tool_names
        assert "ask_user_uiux_question" in tool_names
        # Ticket flow preserved: research delegation, ticket creation, implement offer.
        assert "delegate_task" in tool_names
        assert "delegate_research" in tool_names
        assert "write_final_ticket" in tool_names
        assert "offer_implementation" in tool_names

    def test_implementation_offer_guarantee_middleware_reused(self):
        """The deterministic 'offer to implement' guarantee carries over from Ticket Mode."""
        from app.agents.leonardo.rails_ticket_plan_mode_agent.middleware import (
            ensure_implementation_offer,
            inject_ticket_plan_mode_context,
        )

        assert ensure_implementation_offer is not None
        assert inject_ticket_plan_mode_context is not None


class TestTicketPlanRouting:
    """Frontend selects this agent only when ticket+plan are both on."""

    def test_config_maps_ticket_plan_mode(self):
        config = (FRONTEND / "config.js").read_text()
        assert "ticket_plan: 'rails_ticket_plan_mode_agent'" in config

    def test_index_routes_ticket_to_ticket_plan_agent(self):
        index = (FRONTEND / "index.js").read_text()
        # Plan toggle in ticket mode must resolve to the ticket-plan agent,
        # not silently drop to the generic plan agent.
        assert "ticket: 'rails_ticket_plan_mode_agent'" in index
