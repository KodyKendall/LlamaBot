"""LP-14: Leo schedules plain Ruby jobs with Solid Queue, not cron or an AI job.

The directive is appended as a SUFFIX to rails_agent's system prompt, after the
resolved base prompt, so it reaches the model even on a box where a
mothership-delivered prompt override replaces prompts.py wholesale.
"""
from unittest.mock import patch

from app.agents.leonardo.rails_agent import nodes
from app.agents.leonardo.rails_agent.prompts import SCHEDULED_RAILS_JOBS_DIRECTIVE


def _prompt_text():
    message = nodes.get_cached_system_prompt()
    return message.content[0]["text"]


def test_rails_agent_prompt_carries_the_directive():
    assert SCHEDULED_RAILS_JOBS_DIRECTIVE.strip() in _prompt_text()


def test_directive_survives_a_mothership_prompt_override():
    with patch(
        "app.agents.leonardo.project_context.resolve_base_prompt",
        return_value="OVERRIDDEN BASE PROMPT",
    ):
        text = _prompt_text()

    assert text.startswith("OVERRIDDEN BASE PROMPT")
    assert SCHEDULED_RAILS_JOBS_DIRECTIVE.strip() in text


def test_directive_names_the_pieces_leo_must_use():
    d = SCHEDULED_RAILS_JOBS_DIRECTIVE
    assert "app/jobs/" in d
    assert "db/recurring.yml" in d
    assert "limits_concurrency" in d
    assert "bin/rails restart" in d
    # The things it must steer Leo away from.
    assert "cron" in d
    assert "rails runner" in d
