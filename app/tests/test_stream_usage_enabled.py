"""Every OpenAI-compatible client must stream usage, or compaction is blind.

``ChatOpenAI`` only asks for ``stream_options: {include_usage: true}`` when
``stream_usage`` is True, and it leaves it unset for custom ``base_url`` clients
(Muse, GMI, Fireworks, RunPod, OpenRouter, ...). Without it a streamed turn
carries no ``usage_metadata``, so the provider-reported trigger in
``RailsSummarizationMiddleware`` has nothing to read and turn telemetry reports
zero input tokens. It is set post-construction, like ``stream_chunk_timeout``,
so a new call site cannot forget it.

Same fixture shape as test_provider_key_never_leaks_openai: MODEL_SWITCHING and
an explicit allow-list so policy does not swap the model under test.
"""
import pytest

from app.agents.leonardo.llm_factory import get_llm

OPENAI_COMPATIBLE = [
    ("muse-spark-1.2-contributor", "META_API_KEY"),
    ("muse-spark-1.3-contributor", "META_API_KEY"),
    ("deepseek-v4-flash", "DEEPSEEK_API_KEY"),
    ("deepseek-v4-flash-fireworks", "FIREWORKS_DEEPSEEK_API_KEY"),
    ("deepseek-v4.1-flash-fireworks", "FIREWORKS_DEEPSEEK_API_KEY"),
    ("qwen3-8b-runpod", "RUNPOD_QWEN_API_KEY"),
    ("deepseek-flash-0731-relace", "OPENROUTER_API_KEY"),
]


@pytest.fixture
def _policy_neutral(monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", ",".join(m for m, _ in OPENAI_COMPATIBLE))
    monkeypatch.setenv("RUNPOD_QWEN_BASE_URL", "http://runpod-qwen.invalid/v1")
    monkeypatch.delenv("LLM_STREAM_USAGE", raising=False)


@pytest.mark.parametrize("model,env_var", OPENAI_COMPATIBLE)
def test_openai_compatible_clients_stream_usage(model, env_var, _policy_neutral, monkeypatch):
    monkeypatch.setenv(env_var, "k")
    llm = get_llm(model)
    assert getattr(llm, "stream_usage", None) is True, (
        f"{model} ({type(llm).__name__}) does not stream usage — the reported-usage "
        "compaction trigger and turn telemetry are blind for it"
    )


def test_kill_switch_for_a_provider_that_rejects_stream_options(_policy_neutral, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "k")
    monkeypatch.setenv("LLM_STREAM_USAGE", "false")
    assert getattr(get_llm("muse-spark-1.2-contributor"), "stream_usage", None) is not True


def test_clients_without_the_field_are_left_alone(_policy_neutral):
    """A client with no ``stream_usage`` field is returned untouched, never raised on."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.agents.leonardo.llm_factory import _apply_stream_usage

    llm = FakeListChatModel(responses=["x"])
    assert "stream_usage" not in type(llm).model_fields  # premise of the test
    assert _apply_stream_usage(llm) is llm
    assert not hasattr(llm, "stream_usage")
