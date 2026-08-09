"""The theme rule must be present in every UI-building mode's prompt.

Colors are defined ONCE, as DaisyUI theme variables in
``app/assets/stylesheets/application.css``. An agent that writes a hex code into
a view breaks that inheritance silently — the page looks right today and drifts
from every future brand change, and hardcoded light-mode colors (``bg-white``,
``text-gray-900``) break dark mode outright.

These are structural assertions (does the rule reach the prompt), never
assertions about model output — see the "never unit-TDD LLM behavior" rule.
"""

from unittest.mock import patch

import pytest

from app.agents.leonardo import project_context
from app.agents.leonardo.rails_agent.prompts import RAILS_AGENT_PROMPT
from app.agents.leonardo.rails_beginner_agent.prompts import BEGINNER_AGENT_PROMPT
from app.agents.leonardo.rails_plan_mode_agent.prompts import PLAN_MODE_AGENT_PROMPT

# Engineer plan mode and both delegated sub-agents reuse RAILS_AGENT_PROMPT, so
# covering it here covers them too.
UI_BUILDING_PROMPTS = {
    "engineer (rails_agent)": RAILS_AGENT_PROMPT,
    "beginner (rails_beginner_agent)": BEGINNER_AGENT_PROMPT,
    "plan (rails_plan_mode_agent)": PLAN_MODE_AGENT_PROMPT,
}


@pytest.mark.parametrize("mode,prompt", UI_BUILDING_PROMPTS.items())
def test_prompt_points_at_the_stylesheet_as_the_place_colors_live(mode, prompt):
    assert "app/assets/stylesheets/application.css" in prompt, (
        f"{mode} never tells the agent where colors are defined, so a "
        "'change the brand color' request gets answered by restyling a page."
    )


@pytest.mark.parametrize("mode,prompt", UI_BUILDING_PROMPTS.items())
def test_prompt_names_both_themes(mode, prompt):
    # Editing only the light theme is the most likely way to get this half-right.
    assert '[data-theme="llamapress"]' in prompt, f"{mode} omits the light theme"
    assert '[data-theme="llamapress-dark"]' in prompt, f"{mode} omits the dark theme"


@pytest.mark.parametrize("mode,prompt", UI_BUILDING_PROMPTS.items())
def test_prompt_forbids_hardcoded_colors_in_views(mode, prompt):
    lowered = prompt.lower()
    assert "never hardcode a color" in lowered, f"{mode} lacks the hardcode ban"
    # The two failure modes worth naming explicitly: arbitrary hex, and
    # light-mode-only Tailwind palette colors that break the dark theme.
    assert "bg-[#" in prompt, f"{mode} doesn't name arbitrary hex values"
    assert "bg-white" in prompt, f"{mode} doesn't name the dark-mode breakers"


class TestBrandContextCarriesTheThemeNote:
    """brand.json ships raw hex codes into the prompt on every styling turn.

    Both progressive-disclosure branches must frame them as theme values, or the
    palette reads as paint and defeats the prompt rule above.
    """

    def test_short_guide_is_inlined_with_the_note(self):
        with patch.object(project_context, "get_brand_md_content", return_value="# Brand\nPurple."):
            body = project_context.build_brand_context()
        assert "Purple." in body
        assert project_context.BRAND_THEME_NOTE in body

    def test_long_guide_summary_carries_the_note(self):
        long_md = "x" * (project_context.BRAND_INLINE_THRESHOLD + 1)
        brand_json = {"colors": [{"name": "Primary", "hex": "#5b21b6"}]}
        with patch.object(project_context, "get_brand_md_content", return_value=long_md), \
             patch.object(project_context, "_get_brand_json", return_value=brand_json):
            body = project_context.build_brand_context()
        assert "#5b21b6" in body, "the compact palette should still be injected"
        assert project_context.BRAND_THEME_NOTE in body
        assert "brand-guidelines" in body, "the skill pointer should survive"

    def test_no_brand_guide_still_injects_nothing(self):
        with patch.object(project_context, "get_brand_md_content", return_value=None):
            assert project_context.build_brand_context() is None
            assert project_context.brand_context_section() == ""
