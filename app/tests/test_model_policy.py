"""
Tests for the operator/mothership model policy
(`app.agents.leonardo.model_policy`) and its enforcement in `get_llm()` and the
`/api/available-models` endpoint.

The policy lets us disable models centrally — beyond the existing "is the API key
present?" check — so the instance user cannot select (or re-enable) a model the
operator/mothership has turned off. Resolution order (most-specific wins):

  1. Explicit disable (disabled_models / DISABLED_MODELS) — OFF, beats everything.
  2. Fail-open defaults (deepseek-v4-flash, gemini-3.1-flash-lite) — always ON
     unless explicitly disabled, so every instance keeps a text + vision model.
  3. Allow-list (enabled_models / ENABLED_MODELS) — restricts everything else.
  4. Nothing configured — everything enabled (key-gated, as before).

Sources for the inputs are instance.json (mothership) and env vars (box operator);
allow-lists intersect, disable-lists union. The real enforcement is in get_llm —
the dropdown endpoint only reflects the rule — because the websocket `llm_model`
field is unvalidated user input.
"""
import pytest

from app.agents.leonardo import model_policy
from app.agents.leonardo.llm_factory import (
    DEFAULT_LLM_MODEL,
    ChatDeepSeekWithReasoning,
    get_llm,
)


@pytest.fixture(autouse=True)
def _clean_policy_env(monkeypatch):
    """Default every test to 'nothing configured' unless it opts in."""
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    monkeypatch.delenv("ENABLED_MODELS", raising=False)
    monkeypatch.delenv("DISABLED_MODELS", raising=False)


def _instance(monkeypatch, **config):
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: config)


# --- inert / fail-open -----------------------------------------------------

def test_inert_when_nothing_configured():
    assert model_policy.is_model_enabled("gpt-5-codex") is True
    assert model_policy.is_model_enabled(DEFAULT_LLM_MODEL) is True
    assert model_policy.is_model_enabled("any-future-model") is True


def test_fail_open_defaults_survive_a_restrictive_allowlist(monkeypatch):
    """deepseek + gemini-flash-lite stay on even when an allow-list omits them."""
    monkeypatch.setenv("ENABLED_MODELS", "claude-4.5-sonnet")
    assert model_policy.is_model_enabled("deepseek-v4-flash") is True
    assert model_policy.is_model_enabled("gemini-3.1-flash-lite") is True
    # ...but a non-default, non-listed model is off.
    assert model_policy.is_model_enabled("gpt-5-codex") is False


# --- allow-list ------------------------------------------------------------

def test_env_allowlist_restricts(monkeypatch):
    monkeypatch.setenv("ENABLED_MODELS", "gpt-5-mini, claude-4.5-sonnet")
    assert model_policy.is_model_enabled("gpt-5-mini") is True
    assert model_policy.is_model_enabled("claude-4.5-sonnet") is True
    assert model_policy.is_model_enabled("gpt-5-codex") is False


def test_instance_allowlist_restricts(monkeypatch):
    _instance(monkeypatch, enabled_models=["claude-4.5-sonnet"])
    assert model_policy.is_model_enabled("claude-4.5-sonnet") is True
    assert model_policy.is_model_enabled("gpt-5-codex") is False
    # fail-open default still survives
    assert model_policy.is_model_enabled("deepseek-v4-flash") is True


def test_allow_sources_intersect_neither_can_broaden(monkeypatch):
    """Env allows {A,B}, instance allows {B,C} -> only B (non-default models)."""
    monkeypatch.setenv("ENABLED_MODELS", "claude-4.5-sonnet,gpt-5-mini")
    _instance(monkeypatch, enabled_models=["gpt-5-mini", "gpt-5-codex"])
    assert model_policy.is_model_enabled("gpt-5-mini") is True       # in both
    assert model_policy.is_model_enabled("claude-4.5-sonnet") is False  # env only
    assert model_policy.is_model_enabled("gpt-5-codex") is False     # instance only


# --- explicit disable override ---------------------------------------------

def test_explicit_env_disable_overrides_fail_open(monkeypatch):
    monkeypatch.setenv("DISABLED_MODELS", "gemini-3.1-flash-lite")
    assert model_policy.is_model_enabled("gemini-3.1-flash-lite") is False
    assert model_policy.is_model_enabled("deepseek-v4-flash") is True


def test_explicit_instance_disable_overrides_fail_open(monkeypatch):
    _instance(monkeypatch, disabled_models=["deepseek-v4-flash"])
    assert model_policy.is_model_enabled("deepseek-v4-flash") is False


def test_disable_wins_over_allowlist(monkeypatch):
    monkeypatch.setenv("ENABLED_MODELS", "gpt-5-codex")
    monkeypatch.setenv("DISABLED_MODELS", "gpt-5-codex")
    assert model_policy.is_model_enabled("gpt-5-codex") is False


def test_disable_sources_union(monkeypatch):
    """env disables one, instance disables another -> both off."""
    monkeypatch.setenv("DISABLED_MODELS", "gpt-5-codex")
    _instance(monkeypatch, disabled_models=["gpt-5-mini"])
    assert model_policy.is_model_enabled("gpt-5-codex") is False
    assert model_policy.is_model_enabled("gpt-5-mini") is False


# --- fallback / lockout guard ----------------------------------------------

def test_enabled_default_model_prefers_project_default():
    assert model_policy.enabled_default_model() == DEFAULT_LLM_MODEL


def test_enabled_default_falls_back_to_gemini_when_deepseek_disabled(monkeypatch):
    """Default disabled + allow-list excludes the rest -> the gemini fail-open wins."""
    monkeypatch.setenv("DISABLED_MODELS", "deepseek-v4-flash")
    monkeypatch.setenv("ENABLED_MODELS", "nothing-real")
    assert model_policy.enabled_default_model() == "gemini-3.1-flash-lite"


def test_enabled_default_model_never_locks_out(monkeypatch):
    """Even if the policy disables every known model, a model is still returned."""
    monkeypatch.setenv("DISABLED_MODELS", ",".join(model_policy._KNOWN_MODELS))
    assert model_policy.enabled_default_model() == DEFAULT_LLM_MODEL


# --- enforcement in get_llm (the real gate) --------------------------------

def test_get_llm_replaces_disabled_model(monkeypatch):
    """A disabled model requested via state is swapped for an enabled one, not built."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("ENABLED_MODELS", "deepseek-v4-flash")
    # gpt-5-codex is disabled -> must NOT build a ChatOpenAI; falls back to deepseek.
    llm = get_llm("gpt-5-codex")
    assert isinstance(llm, ChatDeepSeekWithReasoning)


def test_get_llm_builds_enabled_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    llm = get_llm("deepseek-v4-flash")
    assert isinstance(llm, ChatDeepSeekWithReasoning)


# --- /api/available-models reflects the policy -----------------------------

@pytest.mark.asyncio
async def test_available_models_marks_allowlist_disabled(async_client, monkeypatch):
    monkeypatch.setenv("ENABLED_MODELS", "deepseek-v4-flash")
    response = await async_client.get("/api/available-models")
    assert response.status_code == 200
    by_value = {m["value"]: m for m in response.json()["models"]}

    disabled = by_value["gpt-5-codex"]
    assert disabled["available"] is False
    assert disabled["reason"] == "Disabled by administrator"

    # fail-open default is never flagged disabled by an allow-list.
    assert by_value["deepseek-v4-flash"]["reason"] != "Disabled by administrator"
    assert by_value["gemini-3.1-flash-lite"]["reason"] != "Disabled by administrator"


@pytest.mark.asyncio
async def test_available_models_reflects_explicit_disable(async_client, monkeypatch):
    """An explicit disable greys out even a fail-open default in the dropdown."""
    monkeypatch.setenv("DISABLED_MODELS", "gemini-3.1-flash-lite")
    response = await async_client.get("/api/available-models")
    by_value = {m["value"]: m for m in response.json()["models"]}
    flagged = by_value["gemini-3.1-flash-lite"]
    assert flagged["available"] is False
    assert flagged["reason"] == "Disabled by administrator"
