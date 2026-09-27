"""GPT-5.6 Luna/Sol were replaced by GPT-6 Luna/Sol (0.7.11).

The dropdown ids changed (`gpt-5.6-luna-chatgpt` -> `gpt-6-luna-chatgpt`, ...),
but two things out in the fleet still name the OLD ids and cannot be updated in
the same release as this code:

  * the mothership's pushed model_policy.json (enabled/disabled lists), and
  * users' 365-day llmModel cookie.

So an old id is an alias for its replacement: a policy that names it covers the
new id, and a request for it runs on the new model.
"""

import pytest

from app.agents.leonardo import model_policy

RENAMES = {
    "gpt-5.6-luna": "gpt-6-luna",
    "gpt-5.6-luna-chatgpt": "gpt-6-luna-chatgpt",
    "gpt-5.6-sol-chatgpt": "gpt-6-sol-chatgpt",
}


@pytest.fixture(autouse=True)
def _unconfigured_box(monkeypatch):
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {})
    for var in ("ENABLED_MODELS", "DISABLED_MODELS", "MODEL_SWITCHING_ALLOWED"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("old,new", RENAMES.items())
def test_old_id_canonicalizes_to_the_gpt6_id(old, new):
    assert model_policy.canonical_model_name(old) == new


def test_current_ids_are_left_alone():
    for name in ("gpt-6-luna-chatgpt", "deepseek-v4-flash", "gpt-5-mini", ""):
        assert model_policy.canonical_model_name(name) == name


@pytest.mark.parametrize("old,new", RENAMES.items())
def test_an_allow_list_naming_the_old_id_enables_the_new_one(monkeypatch, old, new):
    """The mothership's pushed enabled_models still says gpt-5.6-*."""
    monkeypatch.setattr(
        model_policy, "remote_policy",
        lambda: {"enabled_models": ["deepseek-v4-flash", old]},
    )
    assert model_policy.is_model_enabled(new) is True


@pytest.mark.parametrize("old,new", RENAMES.items())
def test_a_disable_naming_the_old_id_still_disables_the_new_one(monkeypatch, old, new):
    monkeypatch.setenv("DISABLED_MODELS", old)
    assert model_policy.is_model_enabled(new) is False


@pytest.mark.parametrize(
    "old,new", [(o, n) for o, n in RENAMES.items() if n.endswith("-chatgpt")]
)
def test_a_saved_old_pick_runs_on_the_new_model(old, new):
    """A user whose cookie still says gpt-5.6-luna-chatgpt lands on GPT-6 Luna,
    not on the box default."""
    assert model_policy.effective_model(old) == new
