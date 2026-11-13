"""LP-43: every agent that writes Rails views is pointed at the accessibility guide.

Appended as a SUFFIX, after the resolved base prompt, so it reaches the model even on a
box where a mothership-delivered prompt override replaces prompts.py wholesale.
"""
from unittest.mock import patch

import pytest

from app.agents.leonardo.accessible_views import ACCESSIBLE_VIEWS_DIRECTIVE
from app.agents.leonardo.rails_agent import nodes as rails_agent_nodes
from app.agents.leonardo.rails_ai_builder_agent import nodes as ai_builder_nodes
from app.agents.leonardo.rails_beginner_agent import nodes as beginner_nodes

AGENTS = {
    "rails_agent": rails_agent_nodes,
    "rails_beginner_agent": beginner_nodes,
    "rails_ai_builder_agent": ai_builder_nodes,
}


def _prompt_text(module):
    # rails_agent caches a SystemMessage; the beginner and AI builder rebuild a dict each turn.
    if hasattr(module, "get_cached_system_prompt"):
        content = module.get_cached_system_prompt().content
    else:
        content = module.get_sys_msg()["content"]
    return content if isinstance(content, str) else "".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    )


@pytest.mark.parametrize("name", AGENTS)
def test_view_writing_agents_carry_the_directive(name):
    assert ACCESSIBLE_VIEWS_DIRECTIVE.strip() in _prompt_text(AGENTS[name])


@pytest.mark.parametrize("name", AGENTS)
def test_directive_survives_a_mothership_prompt_override(name):
    with patch(
        "app.agents.leonardo.project_context.resolve_base_prompt",
        return_value="OVERRIDDEN BASE PROMPT",
    ):
        text = _prompt_text(AGENTS[name])
    assert "OVERRIDDEN BASE PROMPT" in text
    assert ACCESSIBLE_VIEWS_DIRECTIVE.strip() in text


def test_directive_names_the_guide_and_the_pieces_leo_must_use():
    d = ACCESSIBLE_VIEWS_DIRECTIVE
    assert "Before you write or change a view, follow https://llamapress.ai/cookbook/accessible-views-wcag-aa ." in d
    assert "LlamaA11y.announce" in d
    assert "llama-live-region" in d
    assert "llama_bot_rails/testing/axe" in d
