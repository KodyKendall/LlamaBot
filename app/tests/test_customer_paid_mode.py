"""Customer-paid turns never spend our keys (0.7.11).

Two tickets, one rule:

* Paywall (Darren, 2026-09-23): a customer who is out of messages or spend can
  always keep going on their own ChatGPT account. The gate used to run before the
  model was read, so switching to ChatGPT did nothing.
* Base plan (2026-09-22): ``customer_paid_only`` boxes run only on the customer's
  ChatGPT plan. With no account connected a turn silently ran on DeepSeek, on our key.

On a customer-paid turn a missing credential must raise ChatGPTNotConnected (shown
as a plain "connect your account" message), never fall back to a platform model.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.agents.leonardo import customer_paid, llm_factory, model_policy
from app.services import model_policy_store

LUNA = "gpt-6-luna-chatgpt"
SOL = "gpt-6-sol-chatgpt"

BASE_POLICY = {
    "instance_overrides": {
        "customer_paid_only": True,
        "default_model": LUNA,
        "enabled_models": [LUNA, SOL],
        "roles": {"chat": [LUNA, SOL], "vision": [LUNA]},
    }
}

_PROVIDER_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY", "FIREWORKS_API_KEY", "GMI_API_KEY", "META_API_KEY",
)


@pytest.fixture(autouse=True)
def _box(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_POLICY_PATH", str(tmp_path / "model_policy.json"))
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    for var in ("ENABLED_MODELS", "DISABLED_MODELS", "DEFAULT_LLM_MODEL",
                "VISION_MODEL_ALLOWED", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    for var in _PROVIDER_KEYS:
        monkeypatch.setenv(var, "sk-real-looking-" + var.lower())
    yield


@pytest.fixture
def no_credential(monkeypatch):
    monkeypatch.setattr(llm_factory, "_chatgpt_subscription_client", lambda name: None)


@pytest.fixture
def credential(monkeypatch):
    built = []

    def _client(name):
        built.append(name)
        return SimpleNamespace(chatgpt_client_for=name)

    monkeypatch.setattr(llm_factory, "_chatgpt_subscription_client", _client)
    return built


@pytest.fixture
def platform_builds(monkeypatch):
    """Records every platform-paid client get_llm would build."""
    built = []
    real = llm_factory._build_client

    def _build(name):
        if name not in llm_factory._CHATGPT_SUBSCRIPTION_MODELS:
            built.append(name)
        return real(name)

    monkeypatch.setattr(llm_factory, "_build_client", _build)
    return built


def _push(policy):
    from app.services.lease_manager import LeaseManager

    LeaseManager._sync_model_policy(object.__new__(LeaseManager), {"model_policy": policy})


def _in_customer_paid_turn(fn):
    """Run fn inside a fresh context marked customer-paid, like one websocket turn."""
    import contextvars

    def _run():
        customer_paid.start_customer_paid_turn()
        return fn()

    return contextvars.copy_context().run(_run)


def _raises_not_connected(model):
    with pytest.raises(customer_paid.ChatGPTNotConnected):
        model.invoke("hi")


# ---------------------------------------------------------------------------
# The policy key
# ---------------------------------------------------------------------------

def test_customer_paid_only_survives_the_push_per_instance():
    _push(BASE_POLICY)
    assert model_policy.remote_policy().get("customer_paid_only") is True
    assert customer_paid.policy_enabled()


def test_customer_paid_only_survives_the_push_fleet_wide():
    _push({"customer_paid_only": True})
    assert customer_paid.policy_enabled()


def test_customer_paid_only_must_be_a_real_boolean():
    _push({"instance_overrides": {"customer_paid_only": "yes"}})
    assert not customer_paid.policy_enabled()


def test_a_box_without_the_key_is_not_customer_paid():
    assert not customer_paid.required()


# ---------------------------------------------------------------------------
# Base plan box (customer_paid_only)
# ---------------------------------------------------------------------------

def test_base_box_with_no_chatgpt_never_builds_a_platform_model(no_credential, platform_builds):
    _push(BASE_POLICY)
    for name in (LUNA, "deepseek-v4-flash", "gpt-5-mini", "gemini-3-flash"):
        _raises_not_connected(llm_factory.get_llm(name))
    assert platform_builds == []


def test_base_box_with_chatgpt_runs_on_it(credential, platform_builds):
    _push(BASE_POLICY)
    llm = llm_factory.get_llm("deepseek-v4-flash")
    assert llm.chatgpt_client_for in (LUNA, SOL)
    assert platform_builds == []


def test_base_box_default_and_fallback_are_chatgpt_only():
    _push(BASE_POLICY)
    assert model_policy.enabled_default_model() in (LUNA, SOL)
    assert model_policy.fallback_model(LUNA) == SOL
    assert model_policy.fallback_model(LUNA, exclude={SOL}) is None


def test_box_without_the_key_still_falls_back_to_the_default(no_credential):
    llm = llm_factory.get_llm(LUNA)
    assert not isinstance(llm, customer_paid.NotConnectedChatModel)


def test_graphs_still_compile_on_a_base_box_with_no_chatgpt(no_credential):
    _push(BASE_POLICY)
    llm = llm_factory.get_llm(model_policy.enabled_default_model())
    assert llm.bind_tools([]) is not None


# ---------------------------------------------------------------------------
# One customer-paid turn (the paywall exemption) on an ordinary box
# ---------------------------------------------------------------------------

def test_exempted_turn_with_no_credential_does_not_fall_back(no_credential, platform_builds):
    llm = _in_customer_paid_turn(lambda: llm_factory.get_llm(LUNA))
    _raises_not_connected(llm)
    assert platform_builds == []


def test_exempted_turn_never_builds_a_platform_model_for_sub_calls(credential, platform_builds):
    llm = _in_customer_paid_turn(lambda: llm_factory.get_llm("deepseek-v4-flash"))
    assert llm.chatgpt_client_for == LUNA
    assert platform_builds == []


def test_exempted_turn_does_not_leak_into_the_next_turn(no_credential):
    _in_customer_paid_turn(lambda: None)
    assert not customer_paid.turn_is_customer_paid()


def test_not_connected_is_not_retried_as_transient():
    from app.agents.leonardo.resilience import is_transient_error

    assert not is_transient_error(customer_paid.ChatGPTNotConnected())


# ---------------------------------------------------------------------------
# Titles, summarizer, health probe, chat message
# ---------------------------------------------------------------------------

def test_titles_make_no_model_call_on_a_customer_paid_turn(monkeypatch):
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI",
                        lambda *a, **k: pytest.fail("built a title model on our key"))
    from app.services.thread_service import generate_title_with_llm

    title = _in_customer_paid_turn(
        lambda: asyncio.run(generate_title_with_llm("Build me a gradebook"))
    )
    assert title == "Build me a gradebook"


def _summarizer_labels():
    from app.agents.leonardo.summarization import (
        _ACTIVE_CHAT_MODEL,
        make_summarization_middleware,
    )

    mw = make_summarization_middleware(summary_prompt="x {messages}")
    token = _ACTIVE_CHAT_MODEL.set(LUNA)
    try:
        return [label for label, _ in mw._summarizer_candidates()]
    finally:
        _ACTIVE_CHAT_MODEL.reset(token)


def test_summarizer_uses_only_the_chat_model_on_a_customer_paid_turn(credential):
    assert _in_customer_paid_turn(_summarizer_labels) == [f"chat model {LUNA}"]


def test_summarizer_uses_only_the_chat_model_on_a_base_box(credential):
    _push(BASE_POLICY)
    assert _summarizer_labels() == [f"chat model {LUNA}"]


def test_summarization_model_on_a_base_box_is_not_a_platform_model(no_credential, platform_builds):
    _push(BASE_POLICY)
    model, _, _ = llm_factory.make_summarization_model()
    assert isinstance(model, customer_paid.NotConnectedChatModel)
    assert platform_builds == []


def test_lease_probe_skips_a_chatgpt_default(monkeypatch):
    from app.services.lease_manager import LeaseManager

    _push(BASE_POLICY)
    monkeypatch.setattr(llm_factory, "get_llm",
                        lambda name: pytest.fail("probed a ChatGPT model with no user"))
    asyncio.run(LeaseManager._probe_default_model(object.__new__(LeaseManager)))


def test_chat_shows_the_connect_message_not_the_exception():
    from app.websocket.error_text import chat_error_content

    content = chat_error_content("Error processing request", customer_paid.ChatGPTNotConnected())
    assert content == customer_paid.CONNECT_MESSAGE


def test_the_policy_store_keeps_the_key():
    model_policy_store.save({"customer_paid_only": True, "junk": 1})
    assert model_policy_store.load() == {"customer_paid_only": True}


def test_not_connected_error_frame_offers_the_connect_action():
    from app.websocket.request_handler import RequestHandler

    assert RequestHandler._error_action(customer_paid.ChatGPTNotConnected()) == {
        "action": "connect_chatgpt"
    }
    assert RequestHandler._error_action(RuntimeError("boom")) == {}


def test_llamabot_agent_imports_on_a_box_with_no_openai_key(monkeypatch):
    import importlib

    from app.agents.llamabot import nodes

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    importlib.reload(nodes)
    assert nodes._llm_instance is None
