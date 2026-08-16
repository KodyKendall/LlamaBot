"""No model pointed at a third-party base_url may send OPENAI_API_KEY there.

Every OpenAI-compatible client we build (ChatOpenAI, ChatDeepSeek, ChatQwen) sits
on the openai SDK, and that SDK silently falls back to `OPENAI_API_KEY` from the
environment when it is handed `api_key=None`. Because we override `base_url` to a
third party for these models, an instance that has OPENAI_API_KEY configured but
not the provider's own key would put our OpenAI secret into an
`Authorization: Bearer` header addressed to GMI / Fireworks / Alibaba / Meta.

This was live for `deepseek-v4-flash-gmi`, `deepseek-v4-flash-fireworks` and
`qwen3.7-plus` (verified against the constructed client's `auth_headers`) before
`llm_factory.provider_key` existed. These tests inspect the real underlying SDK
client rather than the LangChain field, because the LangChain field can read
None while the lazily-built SDK client is the thing that carries the header.
"""

import pytest


# Models whose client is deliberately pointed at a non-OpenAI endpoint, with the
# env var that legitimately supplies that provider's key.
THIRD_PARTY_ENDPOINT_MODELS = [
    ("deepseek-v4-flash-gmi", "GMI_DEEPSEEK_API_KEY"),
    ("deepseek-v4-flash-fireworks", "FIREWORKS_DEEPSEEK_API_KEY"),
    ("qwen3.7-plus", "ALIBABA_API_KEY"),
    ("muse-spark-1.2-contributor", "META_API_KEY"),
    # Self-hosted vLLM on our own RunPod GPU. Unauthenticated today, which is
    # exactly why it belongs here: "no key needed" is the case where api_key=None
    # looks harmless and quietly ships OPENAI_API_KEY to the pod's proxy URL.
    ("qwen3-8b-runpod", "RUNPOD_QWEN_API_KEY"),
    ("muse-glimmer-30b-runpod", "RUNPOD_GLIMMER_API_KEY"),
    ("nemotron-lightning-30b-runpod", "RUNPOD_NEMOTRON_API_KEY"),
    # Fireworks serverless. Its key falls back to FIREWORKS_DEEPSEEK_API_KEY,
    # which the fixture below already clears for the DeepSeek row — so the
    # no-key leak case really is keyless here.
    ("nemotron-lightning-30b-fireworks", "FIREWORKS_API_KEY"),
]

SENTINEL = "sk-openai-must-never-leave-this-process"


@pytest.fixture
def no_provider_keys(monkeypatch):
    """An instance with an OpenAI key but none of the third-party provider keys."""
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    # Every model under test is outside the compiled two-model default set, so
    # without an allow-list get_llm would swap it for the default and the leak
    # branch under test would never be reached. Named explicitly rather than
    # widened globally: this file is about what a client sends, not about policy.
    monkeypatch.setenv("ENABLED_MODELS", ",".join(m for m, _ in THIRD_PARTY_ENDPOINT_MODELS))
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    # The self-hosted RunPod entries have no committed default endpoint (the pod
    # URL is sensitive), so give them dummy ones — otherwise the clients under
    # test would be pointed at api.openai.com and the leak they guard against
    # could not happen.
    monkeypatch.setenv("RUNPOD_QWEN_BASE_URL", "http://runpod-qwen.invalid/v1")
    monkeypatch.setenv("RUNPOD_GLIMMER_BASE_URL", "http://runpod-glimmer.invalid/v1")
    monkeypatch.setenv("RUNPOD_NEMOTRON_BASE_URL", "http://runpod-nemotron.invalid/v1")
    for _, env_var in THIRD_PARTY_ENDPOINT_MODELS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)


def _auth_material(llm) -> str:
    """Everything that could end up in the outgoing Authorization header.

    Some clients build the underlying openai SDK client eagerly (ChatDeepSeek,
    ChatQwen) and some build it lazily on first use (ChatOpenAI). Never skip on
    the lazy case — a `None` api_key field IS the leak precondition, since that
    is exactly what gets handed to the SDK later. So check the SDK client when it
    exists, and always check the field, treating an unset field as the leak.
    """
    parts = []

    client = getattr(llm, "root_client", None)
    if client is not None:
        parts.append(str(client.auth_headers))

    key = getattr(llm, "openai_api_key", None) or getattr(llm, "api_key", None)
    if key is None:
        # No key set -> the openai SDK will read OPENAI_API_KEY itself at call time.
        parts.append(SENTINEL)
    else:
        parts.append(key.get_secret_value() if hasattr(key, "get_secret_value") else str(key))

    return " | ".join(parts)


@pytest.mark.parametrize("model,_env", THIRD_PARTY_ENDPOINT_MODELS)
def test_missing_provider_key_does_not_send_the_openai_key(model, _env, no_provider_keys):
    from app.agents.leonardo.llm_factory import get_llm

    assert SENTINEL not in _auth_material(get_llm(model)), (
        f"{model} would send OPENAI_API_KEY to its third-party endpoint — use "
        "llm_factory.provider_key() so the SDK never falls back to OPENAI_API_KEY"
    )


@pytest.mark.parametrize("model,env_var", THIRD_PARTY_ENDPOINT_MODELS)
def test_configured_provider_key_is_the_one_actually_used(model, env_var, no_provider_keys, monkeypatch):
    """The guard must not shadow a real key — the provider's own key still wins."""
    monkeypatch.setenv(env_var, f"real-{env_var}-value")

    from app.agents.leonardo.llm_factory import get_llm

    assert f"real-{env_var}-value" in _auth_material(get_llm(model))


def test_provider_key_prefers_earlier_env_vars(monkeypatch):
    from app.agents.leonardo.llm_factory import provider_key

    monkeypatch.setenv("FIRST_KEY", "first")
    monkeypatch.setenv("SECOND_KEY", "second")
    assert provider_key("FIRST_KEY", "SECOND_KEY") == "first"

    monkeypatch.delenv("FIRST_KEY")
    assert provider_key("FIRST_KEY", "SECOND_KEY") == "second"


def test_provider_key_treats_blank_as_missing(monkeypatch):
    """An empty/whitespace value in .env must not count as configured.

    An empty string would be handed to the SDK as a falsy api_key, which puts us
    straight back on the OPENAI_API_KEY fallback path.
    """
    from app.agents.leonardo.llm_factory import provider_key

    monkeypatch.setenv("BLANK_KEY", "   ")
    result = provider_key("BLANK_KEY")

    assert result.strip(), "provider_key must never return a falsy/blank api_key"
    assert result != "   "
