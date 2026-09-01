"""0.7.6: the fleet default model becomes operator- and mothership-settable.

Meta retired ``muse-spark-1.2-contributor`` on 2026-08-31 at 4:46 PM MDT. It was
the compiled ``DEFAULT_LLM_MODEL`` and carried 89% of fleet turns, so every box
on it went down at once — and nothing in the box could be told to run something
else, because the default was a bare Python constant. Four layers close that:

  1. **Config-settable default.** ``DEFAULT_LLM_MODEL`` / ``FALLBACK_TEXT_MODEL``
     read the environment, and the mothership can override both remotely through
     :mod:`app.services.model_policy_store`. Precedence, most specific first:
     mothership > instance.json > env > compiled constant.
  2. **Automatic reroute.** A retired model raises a deterministic 404, which the
     resilience ladder classified as "do not retry, do not fall back" and
     surfaced raw to the customer. It now routes straight to rung 2 and the dead
     model is remembered, so later turns skip it without failing first.
  3. **Real health probe.** ``GET /v1/models`` still listed the retired model, so
     every health check reported green. Only a real completion tells the truth.
  4. **Remote control.** The mothership sets all of the above over an
     authenticated endpoint instead of SSHing in and editing .env.

The invariant that outranks all of it: **the box always resolves to a model it
can actually build.** A bad remote default must degrade, never lock chat out.
"""

import pytest

from app.agents.leonardo import model_policy
from app.agents.leonardo import model_health
from app.agents.leonardo import llm_factory
from app.agents.leonardo.resilience import is_model_gone, is_transient_error

MUSE = "muse-spark-1.2-contributor"
DEEPSEEK = "deepseek-v4-flash"
GLM = "glm-5.3-flash-zai"


@pytest.fixture(autouse=True)
def _unconfigured_box(monkeypatch):
    """A box that configures nothing, with no remote policy and no dead models."""
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {})
    model_health.reset()
    for var in (
        "ENABLED_MODELS",
        "DISABLED_MODELS",
        "DEFAULT_LLM_MODEL",
        "FALLBACK_TEXT_MODEL",
        "MODEL_SWITCHING_ALLOWED",
        "VISION_MODEL_ALLOWED",
        "META_API_KEY",
        "MODEL_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    model_health.reset()


def _glm_box(monkeypatch):
    """A box keyed for GLM and told to run it, with DeepSeek banned outright."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", GLM)


# --- 1. Nothing changes on a box that sets nothing --------------------------
#
# Every other test here configures something. This one is the control: the
# compiled constants are still Muse and DeepSeek, and an unconfigured box still
# resolves exactly as it did in 0.7.5.


def test_the_compiled_default_is_still_muse():
    assert llm_factory.DEFAULT_LLM_MODEL == MUSE
    assert llm_factory.FALLBACK_TEXT_MODEL == DEEPSEEK


def test_an_unset_override_changes_nothing(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    assert llm_factory.default_llm_model() == MUSE
    assert llm_factory.fallback_text_model() == DEEPSEEK
    assert model_policy.enabled_default_model() == MUSE


# --- 2. The operator sets the default in .env -------------------------------


def test_env_sets_the_default_model(monkeypatch):
    _glm_box(monkeypatch)
    assert llm_factory.default_llm_model() == GLM
    assert model_policy.enabled_default_model() == GLM


def test_env_sets_the_fallback_model(monkeypatch):
    monkeypatch.setenv("FALLBACK_TEXT_MODEL", GLM)
    assert llm_factory.fallback_text_model() == GLM


def test_a_configured_openrouter_default_survives_the_walk(monkeypatch):
    """The skip that made this impossible is scoped to the INCIDENTAL walk.

    ``enabled_default_model`` refuses to wander onto a registered OpenRouter
    endpoint someone added to try. That rule is right, and it is why the fleet
    could not be moved to GLM from config. The operator naming one as THE default
    is the opposite of wandering onto it, so only that name is exempt.
    """
    _glm_box(monkeypatch)
    monkeypatch.setenv("DISABLED_MODELS", f"{MUSE},{DEEPSEEK}")
    assert model_policy.enabled_default_model() == GLM


def test_a_cookie_pinned_user_moves_off_the_retired_model(monkeypatch):
    """The 365-day llmModel cookie is the ONLY thing that reaches these users.

    Muse stops being fail-open once the operator names a different default, which
    is what makes the substitution fire for a user whose browser still asks for
    the retired model by name.
    """
    _glm_box(monkeypatch)
    assert model_policy.effective_model(MUSE) == GLM


def test_the_configured_default_never_resolves_to_a_banned_model(monkeypatch):
    """The bug measured on leo-mize: disabling DeepSeek SERVED DeepSeek.

    With every model disabled, the lockout path returned the compiled
    ``FALLBACK_TEXT_MODEL`` and ignored the disable list entirely, so banning
    DeepSeek fleet-wide would have put every box on it.
    """
    _glm_box(monkeypatch)
    monkeypatch.setenv("DISABLED_MODELS", f"{MUSE},{DEEPSEEK},deepseek-v4-pro")
    assert model_policy.enabled_default_model() == GLM
    assert model_policy.effective_model(DEEPSEEK) == GLM
    assert model_policy.effective_model(MUSE) == GLM


def test_the_configured_default_cannot_be_disabled_out_from_under_itself(monkeypatch):
    """An operator who names a default AND disables it has contradicted himself.

    The default wins, because the alternative is a box with no model at all. Note
    this applies only to an EXPLICITLY configured default — disabling the
    compiled default keeps its old meaning, covered below.
    """
    _glm_box(monkeypatch)
    monkeypatch.setenv("DISABLED_MODELS", GLM)
    assert model_policy.is_model_enabled(GLM) is True
    assert model_policy.enabled_default_model() == GLM


def test_disabling_the_compiled_default_still_works(monkeypatch):
    """0.7.5 behavior, unchanged: no configured default, so disable still wins."""
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DISABLED_MODELS", MUSE)
    assert model_policy.is_model_enabled(MUSE) is False


# --- 3. A default the box cannot build must degrade, not lock chat out ------


def test_a_keyless_openrouter_default_degrades(monkeypatch):
    """``has_provider_key`` reported True for every OpenRouter model.

    It answers from a hand-maintained map and returns True for anything absent,
    so GLM passed the buildable check on a box with no OpenRouter key. The box
    would have "defaulted" to a model that 401s on every turn.
    """
    monkeypatch.setenv("DEFAULT_LLM_MODEL", GLM)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    assert llm_factory.has_provider_key(GLM) is False
    assert model_policy.default_text_model() == DEEPSEEK
    assert model_policy.enabled_default_model() == DEEPSEEK


def test_an_openrouter_default_is_buildable_with_the_key(monkeypatch):
    _glm_box(monkeypatch)
    assert llm_factory.has_provider_key(GLM) is True
    assert model_policy.default_text_model() == GLM


def test_a_nonsense_default_degrades_to_something_runnable(monkeypatch):
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "not-a-real-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    assert model_policy.enabled_default_model() == DEEPSEEK


def test_the_box_is_never_left_without_a_model(monkeypatch):
    """Belt and braces: every knob hostile at once still yields a model name."""
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "not-a-real-model")
    monkeypatch.setenv("DISABLED_MODELS", ",".join(model_policy.known_models()))
    assert model_policy.enabled_default_model()


# --- 4. The mothership sets the default remotely ----------------------------


def test_remote_policy_sets_the_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {"default_model": GLM})
    assert model_policy.enabled_default_model() == GLM


def test_remote_policy_outranks_the_env_override(monkeypatch):
    """The mothership is the operator of record on a fleet box.

    A stale .env on a box the mothership is actively steering must not win, or a
    remote fix silently no-ops on exactly the boxes that were hand-edited during
    the last incident.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", MUSE)
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {"default_model": GLM})
    assert model_policy.enabled_default_model() == GLM


def test_remote_policy_can_disable_a_model(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setattr(
        model_policy, "remote_policy", lambda: {"disabled_models": [DEEPSEEK]}
    )
    assert model_policy.is_model_enabled(DEEPSEEK) is False
    assert model_policy.is_model_enabled(MUSE) is True


def test_remote_disables_union_with_local_ones(monkeypatch):
    """Any source can disable — the 0.7.5 rule, extended to the remote source."""
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DISABLED_MODELS", MUSE)
    monkeypatch.setattr(
        model_policy, "remote_policy", lambda: {"disabled_models": [DEEPSEEK]}
    )
    assert model_policy.is_model_enabled(MUSE) is False
    assert model_policy.is_model_enabled(DEEPSEEK) is False


def test_remote_allowlist_intersects_with_the_local_one(monkeypatch):
    """Neither source can broaden what the other restricts — also the 0.7.5 rule."""
    monkeypatch.setenv("ENABLED_MODELS", f"{MUSE},gpt-5-mini")
    monkeypatch.setattr(
        model_policy, "remote_policy", lambda: {"enabled_models": [MUSE, "gpt-5-nano"]}
    )
    assert model_policy.is_model_enabled("gpt-5-mini") is False
    assert model_policy.is_model_enabled("gpt-5-nano") is False


def test_a_malformed_remote_policy_is_ignored(monkeypatch):
    """A bad remote payload must never take chat down on every box at once.

    This is the blast radius that makes the remote channel worth being careful
    about: one bad value reaches the whole fleet in a single lease interval.
    """
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setattr(
        model_policy,
        "remote_policy",
        lambda: {"default_model": 42, "disabled_models": "not-a-list"},
    )
    assert model_policy.enabled_default_model() == MUSE


# --- 5. A retired model is recognised and rerouted --------------------------


class _NotFound(Exception):
    def __init__(self, message="model_not_found"):
        super().__init__(message)
        self.status_code = 404


def test_a_404_is_classified_as_a_dead_model():
    assert is_model_gone(_NotFound()) is True


def test_a_dead_model_is_still_not_worth_retrying():
    """Rung 1 must not touch it — retrying a retired model is pure latency.

    Five attempts at 25s each is the ~10 minutes of silence the 2026-08-26 stall
    incident was about; a 404 is known-permanent on the first response.
    """
    assert is_transient_error(_NotFound()) is False


def test_a_model_not_found_message_without_a_status_counts():
    """OpenRouter reports upstream failures as a bare ValueError with a dict arg."""
    assert is_model_gone(ValueError({"message": "model_not_found"})) is True


def test_an_ordinary_failure_is_not_a_dead_model():
    assert is_model_gone(TimeoutError("stalled")) is False
    assert is_model_gone(ValueError({"code": 502})) is False


def test_a_dead_model_is_skipped_by_later_turns(monkeypatch):
    """Remembering the death is what stops every later turn paying for it again.

    Without this, each turn re-discovers the 404, waits for it, announces a
    fallback and only then answers — for as long as the retirement lasts.
    """
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    assert model_policy.enabled_default_model() == MUSE
    model_health.mark_model_gone(MUSE)
    assert model_policy.enabled_default_model() == DEEPSEEK


def test_a_dead_model_is_never_chosen_as_a_fallback(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    model_health.mark_model_gone(DEEPSEEK)
    assert model_policy.fallback_model(MUSE) != DEEPSEEK


def test_every_model_dead_still_yields_a_model(monkeypatch):
    """Fail open. A wrong death record must not be more damaging than the outage."""
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    for name in model_policy.known_models():
        model_health.mark_model_gone(name)
    assert model_policy.enabled_default_model()


def test_a_death_record_expires(monkeypatch):
    """A retirement can be reversed, and a 404 can be the provider having a bad day."""
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    model_health.mark_model_gone(MUSE)
    assert model_health.is_gone(MUSE) is True
    later = model_health._now() + model_health.TTL_SECONDS + 1
    monkeypatch.setattr(model_health, "_now", lambda: later)
    assert model_health.is_gone(MUSE) is False
