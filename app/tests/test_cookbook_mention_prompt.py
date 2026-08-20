"""The /cookbook slash menu drops a short "@cookbook:<slug> (<url>.json)" reference
into whatever the user was already typing, instead of replacing the composer with a
paragraph of instructions. That only works if every agent that can act on a cookbook
recipe recognizes the shape — so pin it in each prompt."""

import pytest

PROMPTS = [
    ("app.agents.leonardo.rails_agent.prompts", "RAILS_AGENT_PROMPT"),
    ("app.agents.leonardo.rails_plan_mode_agent.prompts", "PLAN_MODE_AGENT_PROMPT"),
    ("app.agents.leonardo.rails_ticket_mode_agent.prompts", "TICKET_MODE_AGENT_PROMPT"),
    ("app.agents.leonardo.rails_beginner_agent.prompts", "BEGINNER_AGENT_PROMPT"),
]


@pytest.mark.parametrize("module_path,name", PROMPTS)
def test_prompt_explains_the_cookbook_mention(module_path, name):
    module = __import__(module_path, fromlist=[name])
    prompt = getattr(module, name)

    assert "@cookbook:" in prompt, f"{name} can't recognize a picked recipe"
    # The reference carries the JSON URL; the agent must be told to go read it.
    assert "curl" in prompt
    assert ".json" in prompt
