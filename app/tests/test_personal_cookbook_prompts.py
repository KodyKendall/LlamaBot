"""All four Cookbook prompt sections must know personal cookbooks exist (0.7.5).

The agents were told about the FLEET cookbook only. A user who publishes a recipe from one
of their Leos — the feature a Business-plan user with four boxes asked for — would find Leo
on the next box had no idea it existed, could not follow a /cookbook/u/<handle>/<slug>.json
mention, and had no way to save a pattern when asked to.

Structural assertions only: that each prompt names the personal namespace and the publish
guide. Never an assertion about what the model then says.
"""
from pathlib import Path

import pytest

PROMPT_FILES = [
    "rails_agent/prompts.py",
    "rails_plan_mode_agent/prompts.py",
    "rails_ticket_mode_agent/prompts.py",
    "rails_beginner_agent/prompts.py",
]

AGENTS = Path(__file__).resolve().parents[1] / "agents" / "leonardo"


@pytest.mark.parametrize("relative", PROMPT_FILES)
def test_prompt_mentions_the_personal_cookbook_namespace(relative):
    source = (AGENTS / relative).read_text()

    assert "/cookbook/u/" in source, (
        f"{relative} never mentions the personal cookbook namespace, so Leo cannot follow "
        "an @cookbook: mention pointing at one of the user's own recipes."
    )


@pytest.mark.parametrize("relative", PROMPT_FILES)
def test_prompt_points_at_the_publish_guide(relative):
    """'Save this to my cookbook' has to lead somewhere."""
    source = (AGENTS / relative).read_text()

    assert "publish-to-your-personal-cookbook.md" in source, (
        f"{relative} does not tell Leo where the publish flow is documented."
    )


@pytest.mark.parametrize("relative", PROMPT_FILES)
def test_the_fleet_cookbook_is_still_described(relative):
    """The personal cookbook is an addition, not a replacement."""
    source = (AGENTS / relative).read_text()

    assert "cookbook.json" in source
