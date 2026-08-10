"""ChatGPT-subscription models: fail-open, credential isolation, no key leakage.

These pin the three properties that must not regress:

  1. A missing/dead credential degrades to the operator default model, never an
     exception in the middle of a chat turn.
  2. Our OPENAI_API_KEY is never sent to chatgpt.com (the provider_key lesson —
     the openai SDK falls back to the ambient key when handed api_key=None).
  3. One user's subscription never pays for another user's turn.
"""

import pytest

from app.agents.leonardo import llm_factory
from app.agents.leonardo.llm_factory import (
    _CHATGPT_SUBSCRIPTION_MODELS,
    get_llm,
)
from app.agents.leonardo.model_capabilities import MODEL_CAPABILITIES
from app.agents.leonardo.model_policy import _KNOWN_MODELS, enabled_default_model
from app.lib.request_context import (
    current_user_id,
    reset_current_user_id,
    set_current_user_id,
)

SUBSCRIPTION_MODELS = sorted(_CHATGPT_SUBSCRIPTION_MODELS)


@pytest.fixture
def unlocked_models(monkeypatch):
    """Reach the subscription branch of get_llm at all.

    These models ARE in the compiled default enabled set (see
    test_default_model_policy.py for why), so only the switching lock needs
    unsetting here — under it, get_llm would swap the model for the box default
    before the branch under test ever runs.
    """
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.delenv("ENABLED_MODELS", raising=False)
    monkeypatch.delenv("DISABLED_MODELS", raising=False)


def _is_the_boxs_default(llm) -> bool:
    """The fallback is whatever THIS box defaults to, which is env-dependent
    since 0.7.0 (Muse where there is a META key, DeepSeek where there is not).
    The invariant these tests are really about is 'a usable client came back
    instead of an exception', so assert that rather than a fixed class."""
    expected = get_llm(enabled_default_model())
    return type(llm) is type(expected)


@pytest.fixture
def no_user():
    token = set_current_user_id(None)
    yield
    reset_current_user_id(token)


# --- registration ------------------------------------------------------------


@pytest.mark.parametrize("model", SUBSCRIPTION_MODELS)
def test_registered_in_policy_and_capabilities(model):
    """Half-registering a model is the documented trap (model_capabilities.py:57):
    an unlisted name reports vision-capable while get_llm hands back a text-only
    fallback, and images 400 at the provider."""
    assert model in _KNOWN_MODELS
    assert model in MODEL_CAPABILITIES


def test_subscription_models_are_separate_entries_from_api_key_twins():
    """The same model id reachable two ways must be two dropdown entries, or the
    user cannot tell which credential is being billed."""
    for frontend_name, openai_id in _CHATGPT_SUBSCRIPTION_MODELS.items():
        assert frontend_name != openai_id
        assert frontend_name.endswith("-chatgpt")


def test_default_model_never_requires_a_user_credential():
    """enabled_default_model() is the fail-open target; if it ever resolved to a
    subscription model, a user without a connection could not chat at all."""
    from app.agents.leonardo.model_policy import enabled_default_model

    assert enabled_default_model() not in _CHATGPT_SUBSCRIPTION_MODELS


# --- fail open ---------------------------------------------------------------


@pytest.mark.parametrize("model", SUBSCRIPTION_MODELS)
def test_no_connected_account_falls_back_to_default(model, unlocked_models, no_user, monkeypatch):
    """No user in context -> default model, not an exception."""
    monkeypatch.setattr(
        llm_factory, "_chatgpt_subscription_client", lambda _name: None
    )
    llm = get_llm(model)
    assert _is_the_boxs_default(llm)


@pytest.mark.parametrize("model", SUBSCRIPTION_MODELS)
def test_revoked_credential_falls_back_instead_of_raising(model, unlocked_models, monkeypatch):
    """A revoked token must degrade to the box's default, never break the turn."""
    from app.services import chatgpt_auth

    token = set_current_user_id(42)
    try:
        monkeypatch.setattr(
            chatgpt_auth,
            "access_token_for_user_sync",
            lambda _uid: None,
        )
        llm = get_llm(model)
        assert _is_the_boxs_default(llm)
    finally:
        reset_current_user_id(token)


@pytest.mark.parametrize("model", SUBSCRIPTION_MODELS)
def test_credential_lookup_blowing_up_still_returns_a_model(model, unlocked_models, monkeypatch):
    """Any unexpected error in credential resolution is contained."""
    from app.services import chatgpt_auth

    def boom(_uid):
        raise RuntimeError("auth DB is on fire")

    token = set_current_user_id(7)
    try:
        monkeypatch.setattr(chatgpt_auth, "access_token_for_user_sync", boom)
        llm = get_llm(model)
        assert _is_the_boxs_default(llm)
    finally:
        reset_current_user_id(token)


# --- no credential leakage ---------------------------------------------------


@pytest.mark.parametrize("model", SUBSCRIPTION_MODELS)
def test_operator_openai_key_never_reaches_chatgpt_backend(model, unlocked_models, monkeypatch):
    """The provider_key hazard, restated for this endpoint: with no user
    credential we must NOT construct a client at all, because a None api_key
    would make the openai SDK send OPENAI_API_KEY to chatgpt.com."""
    from app.services import chatgpt_auth

    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-OPERATOR-SECRET")
    monkeypatch.setattr(chatgpt_auth, "access_token_for_user_sync", lambda _uid: None)

    token = set_current_user_id(1)
    try:
        llm = get_llm(model)
    finally:
        reset_current_user_id(token)

    # The invariant is about the DESTINATION: with no user credential, no client
    # addressed to chatgpt.com may be constructed at all.
    #
    # Do NOT assert on `openai_api_key` of the fallback client. On ChatDeepSeek
    # that attribute is a vestigial field inherited from BaseChatOpenAI and is
    # populated from the ambient OPENAI_API_KEY, while the request actually uses
    # `api_key` (the DeepSeek key) against api.deepseek.com. Reading it looks
    # like a leak and is not one.
    base_url = str(getattr(llm, "openai_api_base", "") or "")
    assert "chatgpt.com" not in base_url
    assert _is_the_boxs_default(llm)


@pytest.mark.parametrize("model", SUBSCRIPTION_MODELS)
def test_connected_user_gets_their_own_token_and_account_header(model, unlocked_models, monkeypatch):
    from app.services import chatgpt_auth

    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-OPERATOR-SECRET")
    monkeypatch.setattr(
        chatgpt_auth,
        "access_token_for_user_sync",
        lambda _uid: ("user-access-token", "acct_123"),
    )

    token = set_current_user_id(1)
    try:
        llm = get_llm(model)
    finally:
        reset_current_user_id(token)

    secret = getattr(llm, "openai_api_key", None)
    raw = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    assert raw == "user-access-token"
    assert "chatgpt.com" in str(llm.openai_api_base)
    headers = getattr(llm, "default_headers", {}) or {}
    assert headers.get("chatgpt-account-id") == "acct_123"


# --- the Codex backend forbids system messages -------------------------------


def _codex_client():
    from app.agents.leonardo.llm_factory import ChatOpenAICodexBackend

    return ChatOpenAICodexBackend(
        model="gpt-5.6-luna",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="test-token",
        use_responses_api=True,
        output_version="responses/v1",
    )


def test_system_message_is_hoisted_out_of_input():
    """The backend 400s with "System messages are not allowed" on any system-role
    entry; the prompt belongs in top-level `instructions`."""
    from langchain_core.messages import HumanMessage, SystemMessage

    payload = _codex_client()._get_request_payload(
        [SystemMessage(content="You are Leo."), HumanMessage(content="hi")]
    )

    assert all(m.get("role") != "system" for m in payload["input"])
    assert payload["instructions"] == "You are Leo."


def test_multiple_system_messages_are_joined_in_order():
    from langchain_core.messages import HumanMessage, SystemMessage

    payload = _codex_client()._get_request_payload(
        [SystemMessage(content="A"), SystemMessage(content="B"), HumanMessage(content="hi")]
    )
    assert payload["instructions"] == "A\n\nB"


def test_anthropic_style_block_list_system_message_is_flattened():
    """Our system prompts carry cache_control content blocks for Anthropic. Those
    must flatten to text rather than being stringified into the instructions."""
    from langchain_core.messages import HumanMessage, SystemMessage

    payload = _codex_client()._get_request_payload([
        SystemMessage(content=[
            {"type": "text", "text": "Block A", "cache_control": {"type": "ephemeral"}},
        ]),
        HumanMessage(content="hi"),
    ])

    assert payload["instructions"] == "Block A"
    assert "cache_control" not in str(payload.get("instructions"))


def test_conversation_turns_survive_the_rewrite():
    """Only system entries are removed — dropping a turn would silently truncate
    the conversation."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    payload = _codex_client()._get_request_payload([
        SystemMessage(content="sys"),
        HumanMessage(content="one"),
        AIMessage(content="two"),
        HumanMessage(content="three"),
    ])

    assert [m.get("role") for m in payload["input"]] == ["user", "assistant", "user"]


def test_store_is_always_false():
    """The backend 400s with "Store must be set to false". Also our retention
    posture — this endpoint must never leave responses stored server-side."""
    from langchain_core.messages import HumanMessage, SystemMessage

    client = _codex_client()
    with_system = client._get_request_payload(
        [SystemMessage(content="s"), HumanMessage(content="hi")]
    )
    without_system = client._get_request_payload([HumanMessage(content="hi")])

    assert with_system["store"] is False
    assert without_system["store"] is False, (
        "the early return path skipped the store flag, so a system-prompt-less "
        "turn would 400"
    )


def test_payload_untouched_when_there_is_no_system_message():
    from langchain_core.messages import HumanMessage

    payload = _codex_client()._get_request_payload([HumanMessage(content="hi")])
    assert "instructions" not in payload or payload["instructions"] is None
    assert [m.get("role") for m in payload["input"]] == ["user"]


def test_subscription_client_uses_the_codex_backend_subclass(unlocked_models, monkeypatch):
    """A plain ChatOpenAI here would 400 on every turn that has a system prompt —
    which is every turn."""
    from app.agents.leonardo.llm_factory import ChatOpenAICodexBackend
    from app.services import chatgpt_auth

    monkeypatch.setattr(
        chatgpt_auth, "access_token_for_user_sync", lambda _uid: ("tok", "acct")
    )
    token = set_current_user_id(1)
    try:
        llm = get_llm("gpt-5.6-luna-chatgpt")
    finally:
        reset_current_user_id(token)

    assert isinstance(llm, ChatOpenAICodexBackend)


def test_we_identify_as_llamabot_not_codex_cli():
    """We are not the Codex CLI. OpenAI's client supports an originator override,
    so honest identification is a supported path — if this ever has to become
    'codex_cli_rs' that is a product decision, not a silent default."""
    from app.services.chatgpt_auth import ORIGINATOR, default_headers

    assert ORIGINATOR != "codex_cli_rs"
    assert default_headers()["originator"] == ORIGINATOR


# --- per-user isolation ------------------------------------------------------


def test_credential_is_scoped_per_user(unlocked_models, monkeypatch):
    """User A's subscription must never serve user B's turn."""
    from app.services import chatgpt_auth

    tokens_by_user = {1: ("token-user-1", "acct_1"), 2: ("token-user-2", "acct_2")}
    monkeypatch.setattr(
        chatgpt_auth, "access_token_for_user_sync", lambda uid: tokens_by_user.get(uid)
    )

    seen = {}
    for uid in (1, 2):
        token = set_current_user_id(uid)
        try:
            llm = get_llm("gpt-5.6-luna-chatgpt")
            secret = getattr(llm, "openai_api_key", None)
            seen[uid] = (
                secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
            )
        finally:
            reset_current_user_id(token)

    assert seen[1] == "token-user-1"
    assert seen[2] == "token-user-2"


def test_unknown_user_context_defaults_to_none():
    """Fails closed: no ambient user means no subscription spend."""
    assert current_user_id() is None
