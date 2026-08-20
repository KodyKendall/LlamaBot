"""The `/` menu can now be opened mid-sentence, so a picked skill's `/<slug>` token
lands wherever the caret was — not necessarily at the front of the message. A prompt
that still says the message "begins with" the token will miss the pick and answer as
if the user typed a stray path."""

import pytest

PROMPTS = [
    ("app.agents.leonardo.rails_agent.prompts", "RAILS_AGENT_PROMPT"),
    ("app.agents.leonardo.rails_beginner_agent.prompts", "BEGINNER_AGENT_PROMPT"),
]

# Wordings that pin the token to the front of the message.
LEADING_ONLY = [
    "begins with that skill's slash token",
    "starts with that skill's slash token",
    "A leading `/<slug>`",
]


@pytest.mark.parametrize("module_path,name", PROMPTS)
def test_skill_token_is_not_described_as_leading(module_path, name):
    module = __import__(module_path, fromlist=[name])
    prompt = getattr(module, name)

    assert "`/<slug>`" in prompt, f"{name} lost its skill-token section"
    for phrase in LEADING_ONLY:
        assert phrase not in prompt, f"{name} still assumes the token leads: {phrase!r}"
    assert "anywhere in their message" in prompt, (
        f"{name} must say the token can sit anywhere in the message"
    )
