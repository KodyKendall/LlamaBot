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


@pytest.mark.parametrize("relative", PROMPT_FILES)
def test_prompt_does_not_send_leo_to_the_environment_for_the_token(relative):
    """The publish flow must not name a variable the exec scrub blanks (0.7.7).

    ``MOTHERSHIP_API_TOKEN`` is not in ``_EXEC_ENV_ALLOWLIST`` (rails_agent/tools.py),
    and that list is default-deny, so ``build_exec_env`` blanks it for every
    ``bash_command`` exec. Leo read an empty string, sent ``Authorization: Bearer ``
    and the mothership answered ``{"success":false,"error":"Missing credentials"}``.
    The customer on box leo-zuset was told the app "does not have the required
    cookbook sign-in credentials" and left a thumbs-down (2026-09-01).

    The credentials themselves are fine — they are readable from
    ``/rails/.leonardo/instance.json``, which the scrub does not touch, and the
    published guide now documents that path. Only the prompt's pointer was wrong.

    Same failure mode as the ``HOSTED_DOMAIN`` note already in that allowlist:
    "scrubbing it made the documented command return an empty string."
    """
    source = (AGENTS / relative).read_text()

    assert "MOTHERSHIP_API_TOKEN" not in source, (
        f"{relative} sends Leo to an environment variable the exec scrub blanks. "
        "Point at the published guide instead; it documents where the credentials live."
    )
