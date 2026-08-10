"""The 0.7.0 fleet default: Muse Spark 1.2, two enabled models, switching ON.

Four settled decisions are pinned here, because every one of them is a *default*
— i.e. the behavior of a box that configures nothing, which is most of the fleet:

  1. The default model is ``muse-spark-1.2-contributor``.
  2. Two models are enabled out of the box on operator credentials (Muse +
     ``deepseek-v4-flash``). A fleet box carries OpenAI/Google/Anthropic keys for
     other subsystems; those keys must NOT put their models in the dropdown. The
     ChatGPT-subscription entries are the deliberate exception — no operator key
     reaches them, so they cost nothing to leave enabled and are unreachable if
     disabled (Kody, 2026-08-10).
  3. Model switching defaults ON (``MODEL_SWITCHING_ALLOWED`` stays as a per-box
     opt-OUT rather than an opt-in).
  4. Muse is only usable with a META key. Without one the default must resolve to
     ``deepseek-v4-flash`` and turns must still complete — a box must never
     "default" to a model ``get_llm`` cannot build.

The operator overrides (explicit disable, allow-lists, the switching lock) keep
their existing precedence; that ordering is covered in test_model_policy.py.
"""

import pytest

from app.agents.leonardo import model_policy
from app.agents.leonardo.llm_factory import (
    DEFAULT_LLM_MODEL,
    FALLBACK_TEXT_MODEL,
    ChatDeepSeekWithReasoning,
    get_llm,
)

MUSE = "muse-spark-1.2-contributor"


@pytest.fixture(autouse=True)
def _unconfigured_box(monkeypatch):
    """A box that configures nothing: no lists, no gates, no keys.

    Individual tests opt back into whichever key or list they are about. Note
    every gate env var is DELETED rather than set — the point of these tests is
    what happens when a box sets nothing at all.
    """
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    for var in (
        "ENABLED_MODELS",
        "DISABLED_MODELS",
        "MODEL_SWITCHING_ALLOWED",
        "VISION_MODEL_ALLOWED",
        "META_API_KEY",
        "MODEL_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _with_meta_key(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "meta-test-key")


# --- 1. Muse is the default -------------------------------------------------

def test_the_project_default_is_muse():
    assert DEFAULT_LLM_MODEL == MUSE


def test_default_resolves_to_muse_when_the_box_has_a_meta_key(monkeypatch):
    _with_meta_key(monkeypatch)
    assert model_policy.default_text_model() == MUSE
    assert model_policy.enabled_default_model() == MUSE


def test_metas_own_key_name_also_counts(monkeypatch):
    """Meta's docs call the key MODEL_API_KEY; their LiteLLM integration uses
    META_API_KEY. get_llm accepts either, so the usability check must too."""
    monkeypatch.setenv("MODEL_API_KEY", "meta-test-key")
    assert model_policy.default_text_model() == MUSE


# --- 2. Exactly two models enabled by default -------------------------------

def test_only_the_two_blessed_models_are_enabled_by_default(monkeypatch):
    _with_meta_key(monkeypatch)
    assert model_policy.is_model_enabled(MUSE) is True
    assert model_policy.is_model_enabled("deepseek-v4-flash") is True


@pytest.mark.parametrize("model", ["gpt-5.6-luna-chatgpt", "gpt-5.6-sol-chatgpt"])
def test_the_chatgpt_subscription_models_stay_reachable(monkeypatch, model):
    """The one exception to the two-model default, and it is not an oversight.

    These are paid for by the signed-in user's own ChatGPT plan, so no operator
    key puts them in the dropdown — the rule the default set enforces does not
    apply. Disabled by default they would be unreachable on every fleet box:
    /api/available-models greys them out with "Connect your ChatGPT account"
    until a user connects one, so nothing is exposed by leaving them on.
    """
    _with_meta_key(monkeypatch)
    assert model_policy.is_model_enabled(model) is True


@pytest.mark.parametrize("model", ["gpt-5.6-luna-chatgpt", "gpt-5.6-sol-chatgpt"])
def test_a_subscription_model_is_never_chosen_as_the_default(monkeypatch, model):
    """Enabled is not the same as default-able: resolving the box default onto a
    model that needs a per-user credential would leave a user who has connected
    nothing unable to chat at all."""
    monkeypatch.setenv("DISABLED_MODELS", f"{MUSE},deepseek-v4-flash")
    assert model_policy.enabled_default_model() != model


@pytest.mark.parametrize("model", [
    "gpt-5-codex",
    "gpt-5-mini",
    "gpt-5-nano",
    "claude-4.5-sonnet",
    "gemini-3-flash",
    "qwen3.7-plus",
    "deepseek-v4-pro",
])
def test_a_stray_fleet_key_does_not_put_a_model_in_the_dropdown(monkeypatch, model):
    """The regression this guards: switching is ON by default now, and the old
    "no allow-list means everything is enabled" rule would have opened the
    dropdown to every model whose key happens to sit in a fleet box's .env."""
    _with_meta_key(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fleet")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fleet")
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-fleet")
    assert model_policy.is_model_enabled(model) is False


@pytest.mark.asyncio
async def test_available_models_shows_exactly_the_two(async_client, monkeypatch):
    _with_meta_key(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fleet")
    response = await async_client.get("/api/available-models")
    assert response.status_code == 200
    body = response.json()

    enabled = {
        m["value"] for m in body["models"]
        if m["reason"] != "Disabled by administrator"
    }
    assert enabled == {MUSE, "deepseek-v4-flash"}


@pytest.mark.asyncio
async def test_available_models_reports_the_resolved_default(async_client, monkeypatch):
    """The frontend hardcoded 'deepseek-v4-flash' as the model to reset/pin to.
    That constant is now box-dependent, so the backend has to tell it."""
    _with_meta_key(monkeypatch)
    response = await async_client.get("/api/available-models")
    assert response.json()["default_model"] == MUSE

    monkeypatch.delenv("META_API_KEY", raising=False)
    response = await async_client.get("/api/available-models")
    assert response.json()["default_model"] == FALLBACK_TEXT_MODEL


def test_an_operator_allowlist_still_widens_the_default_set(monkeypatch):
    """The two-model set is a DEFAULT, not a ceiling — a box that names its own
    allow-list gets exactly that (plus the fail-open pair)."""
    _with_meta_key(monkeypatch)
    monkeypatch.setenv("ENABLED_MODELS", "claude-4.5-sonnet")
    assert model_policy.is_model_enabled("claude-4.5-sonnet") is True
    assert model_policy.is_model_enabled("gpt-5-codex") is False
    # the blessed pair fails open through it
    assert model_policy.is_model_enabled(MUSE) is True
    assert model_policy.is_model_enabled("deepseek-v4-flash") is True


# --- 3. Model switching is ON by default ------------------------------------

def test_switching_is_allowed_when_the_env_var_is_unset():
    assert model_policy.model_switching_allowed() is True


def test_switching_is_still_an_opt_out(monkeypatch):
    """`MODEL_SWITCHING_ALLOWED=false` must still pin the box to its default —
    compliance boxes rely on it."""
    _with_meta_key(monkeypatch)
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "false")
    assert model_policy.model_switching_allowed() is False
    assert model_policy.is_model_enabled(MUSE) is True
    assert model_policy.is_model_enabled("deepseek-v4-flash") is False
    assert model_policy.is_model_enabled("claude-4.5-sonnet") is False


def test_the_lock_pins_the_model_the_box_can_actually_build(monkeypatch):
    """Locked AND no META key: the pin has to land on DeepSeek, not on a Muse
    the box cannot construct — otherwise the lock bricks chat entirely."""
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "false")
    assert model_policy.is_model_enabled(FALLBACK_TEXT_MODEL) is True
    assert model_policy.enabled_default_model() == FALLBACK_TEXT_MODEL


# --- 4. No META key ⇒ DeepSeek fallback, turns still work -------------------

def test_default_falls_back_to_deepseek_without_a_meta_key():
    assert model_policy.default_text_model() == FALLBACK_TEXT_MODEL
    assert model_policy.enabled_default_model() == FALLBACK_TEXT_MODEL


def test_a_keyless_box_still_builds_a_working_client(monkeypatch):
    """Acceptance 2: a turn completes. get_llm must hand back a buildable client
    rather than raising on an unbuildable default."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    assert isinstance(get_llm(model_policy.enabled_default_model()),
                      ChatDeepSeekWithReasoning)


def test_muse_is_not_silently_used_without_a_key(monkeypatch):
    """A blank/whitespace META_API_KEY is 'absent', not 'configured' — otherwise
    the box defaults to Muse and every turn 401s."""
    monkeypatch.setenv("META_API_KEY", "   ")
    assert model_policy.default_text_model() == FALLBACK_TEXT_MODEL


# --- explicit disable still beats everything, including the new pair --------

def test_explicit_disable_beats_the_new_fail_open_pair(monkeypatch):
    _with_meta_key(monkeypatch)
    monkeypatch.setattr(
        model_policy, "_read_instance_config", lambda: {"disabled_models": [MUSE]}
    )
    assert model_policy.is_model_enabled(MUSE) is False
    # ...and the default moves off it rather than returning a disabled model.
    assert model_policy.enabled_default_model() == FALLBACK_TEXT_MODEL


def test_disabling_both_blessed_models_never_locks_the_box_out(monkeypatch):
    monkeypatch.setenv("DISABLED_MODELS", f"{MUSE},deepseek-v4-flash")
    monkeypatch.setenv("ENABLED_MODELS", "claude-4.5-sonnet")
    # Some concrete model comes back; the box is never left with nothing.
    assert model_policy.enabled_default_model() == "claude-4.5-sonnet"


# --- 5. vision routes through Muse ------------------------------------------

def test_the_vision_auto_switch_target_is_muse():
    assert model_policy.VISION_MODEL == MUSE


def test_muse_is_multimodal():
    from app.agents.leonardo.model_capabilities import get_model_capabilities

    caps = get_model_capabilities(MUSE)
    assert caps["images"] is True


def test_the_fallback_text_model_has_no_vision():
    """The premise behind the 'vision unavailable' message: a box on the DeepSeek
    fallback genuinely cannot see images."""
    from app.agents.leonardo.model_capabilities import get_model_capabilities

    assert get_model_capabilities(FALLBACK_TEXT_MODEL)["images"] is False
