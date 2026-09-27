"""ZDR mode: a zero-data-retention box talks only to its allowed model vendor (0.7.11).

Two independent layers must both say yes before a ZDR box sends data to a model:
the mothership's ``allowed_models`` and LlamaBot's compiled ``ZDR_COMPLIANT``
table. Everything that does not go through ``get_llm`` (chat titles, the
summarizer fallback chain, web search, tracing, analytics, the ChatGPT connect
flow, agents that build a fixed client) is closed separately.

``test_zdr_box_contacts_only_allowed_vendor`` is the test cited in customer
security documentation. Keep its name and keep it plain.
"""
import asyncio
import json

import httpx
import pytest
import requests

from app.agents.leonardo import llm_factory, model_policy, zdr
from app.agents.leonardo.model_capabilities import MODEL_CAPABILITIES, ZDR_COMPLIANT
from app.services import model_policy_store

ALLOWED = "deepseek-v4.1-flash-fireworks"
FIREWORKS_HOST = "api.fireworks.ai"

ZDR_POLICY = {
    "instance_overrides": {
        "default_model": ALLOWED,
        "enabled_models": [ALLOWED],
        "roles": {"chat": [ALLOWED], "vision": [ALLOWED]},
        "zdr": {"enabled": True, "allowed_models": [ALLOWED], "sensitivity": "ferpa"},
    }
}

# Every provider key a real box might hold, all set, so nothing is "safe" only
# because a key happens to be missing.
_PROVIDER_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY", "FIREWORKS_API_KEY", "GMI_API_KEY", "META_API_KEY",
    "MODEL_API_KEY", "ALIBABA_API_KEY", "DASHSCOPE_API_KEY", "HETZNER_API_KEY",
    "RUNPOD_API_KEY", "TAVILY_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY",
)


@pytest.fixture(autouse=True)
def _box(monkeypatch, tmp_path):
    """A box whose pushed policy lives in tmp_path, with every key set."""
    monkeypatch.setenv("MODEL_POLICY_PATH", str(tmp_path / "model_policy.json"))
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    for var in ("ENABLED_MODELS", "DISABLED_MODELS", "DEFAULT_LLM_MODEL",
                "VISION_MODEL_ALLOWED", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    for var in _PROVIDER_KEYS:
        monkeypatch.setenv(var, "sk-real-looking-" + var.lower())
    zdr._reset_for_tests()
    yield
    zdr._reset_for_tests()
    import langsmith
    langsmith.configure(enabled=None)


def _push(policy):
    """What the lease tick does with a pushed model_policy."""
    from app.services.lease_manager import LeaseManager

    LeaseManager._sync_model_policy(object.__new__(LeaseManager), {"model_policy": policy})


def _zdr_on():
    _push(ZDR_POLICY)


# ---------------------------------------------------------------------------
# 1. The flag is carried through the policy
# ---------------------------------------------------------------------------

def test_instance_override_zdr_reaches_the_box():
    _zdr_on()
    state = zdr.zdr_state()
    assert state.enabled
    assert state.allowed_models == (ALLOWED,)
    assert state.sensitivity == "ferpa"
    assert model_policy.remote_policy()["zdr"]["enabled"] is True


def test_fleet_level_zdr_is_ignored():
    _push({"zdr": {"enabled": True, "allowed_models": [ALLOWED]}})
    assert not zdr.zdr_state().enabled


def test_non_zdr_box_is_off():
    _push({"default_model": "deepseek-v4-flash"})
    assert not zdr.zdr_state().enabled


# ---------------------------------------------------------------------------
# 2. Fail closed
# ---------------------------------------------------------------------------

def test_missing_policy_on_a_zdr_box_refuses_model_calls():
    _zdr_on()
    zdr.zdr_state()
    model_policy_store.path().unlink()  # e.g. a wiped .leonardo, mothership down

    state = zdr.zdr_state()
    assert state.enabled and state.locked
    with pytest.raises(zdr.ZDRRefused):
        llm_factory.get_llm("deepseek-v4-flash")


def test_corrupt_policy_on_a_zdr_box_refuses_model_calls():
    _zdr_on()
    model_policy_store.path().write_text("{not json")
    with pytest.raises(zdr.ZDRRefused):
        llm_factory.get_llm(ALLOWED)


def test_half_valid_zdr_block_still_arms_the_lock():
    _push({"instance_overrides": {"zdr": {"enabled": True, "allowed_models": "oops"}}})
    assert zdr.zdr_state().enabled
    with pytest.raises(zdr.ZDRRefused):
        llm_factory.get_llm(ALLOWED)


def test_zdr_turns_off_only_when_the_mothership_drops_the_flag():
    _zdr_on()
    assert zdr.zdr_state().enabled
    _push({"default_model": ALLOWED})  # a document WITHOUT zdr
    assert not zdr.zdr_state().enabled


def test_explicit_empty_policy_also_turns_zdr_off():
    _zdr_on()
    _push({})
    assert not zdr.zdr_state().enabled


def test_a_box_upgraded_with_zdr_already_on_disk_arms_the_lock(monkeypatch):
    """0.7.10 already saved the zdr block; 0.7.11 must lock on first read."""
    model_policy_store.path().write_text(json.dumps(ZDR_POLICY))
    assert zdr.zdr_state().enabled
    model_policy_store.path().unlink()
    assert zdr.zdr_state().enabled


# ---------------------------------------------------------------------------
# 3. get_llm is the choke point; both layers must agree
# ---------------------------------------------------------------------------

def test_get_llm_remaps_every_request_to_the_allowed_model(monkeypatch):
    _zdr_on()
    built = []
    real_build = llm_factory._build_client
    monkeypatch.setattr(llm_factory, "_build_client",
                        lambda name: built.append(name) or real_build(name))
    for name in ("gpt-5-mini", "claude-4.5-haiku", "gemini-3-flash", "deepseek-v4-flash",
                 "muse-spark-1.2-contributor", "gpt-6-luna-chatgpt"):
        llm_factory.get_llm(name)
    assert set(built) == {ALLOWED}


def test_mothership_listing_a_non_compliant_model_does_not_open_it(monkeypatch):
    bad = json.loads(json.dumps(ZDR_POLICY))
    bad["instance_overrides"]["zdr"]["allowed_models"] = [
        "gpt-5-mini", "gpt-6-luna-chatgpt", ALLOWED,
    ]
    bad["instance_overrides"]["enabled_models"] = ["gpt-5-mini", "gpt-6-luna-chatgpt", ALLOWED]
    _push(bad)

    assert not model_policy.is_model_enabled("gpt-5-mini")
    assert not model_policy.is_model_enabled("gpt-6-luna-chatgpt")
    assert model_policy.effective_model("gpt-5-mini") == ALLOWED
    with pytest.raises(zdr.ZDRRefused):
        zdr.check_model("gpt-5-mini")


def test_no_compliant_model_in_the_allowed_list_refuses_the_turn():
    bad = json.loads(json.dumps(ZDR_POLICY))
    bad["instance_overrides"]["zdr"]["allowed_models"] = ["gpt-5-mini"]
    _push(bad)
    with pytest.raises(zdr.ZDRRefused):
        llm_factory.get_llm("gpt-5-mini")


def test_config_registered_entry_is_never_compliant(monkeypatch):
    from app.agents.leonardo import openrouter_models

    monkeypatch.setattr(openrouter_models, "is_openrouter_model", lambda n: n == ALLOWED)
    assert not zdr.is_zdr_compliant(ALLOWED)


def test_chatgpt_client_is_never_built_under_zdr(monkeypatch):
    _zdr_on()
    monkeypatch.setattr(
        "app.services.chatgpt_auth.access_token_for_user_sync",
        lambda uid: pytest.fail("read a ChatGPT credential on a ZDR box"),
    )
    assert llm_factory._chatgpt_subscription_client("gpt-6-luna-chatgpt") is None


def test_every_compiled_model_declares_zdr_compliance_explicitly():
    assert set(ZDR_COMPLIANT) == set(MODEL_CAPABILITIES)
    assert set(model_policy._KNOWN_MODELS) <= set(ZDR_COMPLIANT)
    assert all(isinstance(v, bool) for v in ZDR_COMPLIANT.values())
    assert [m for m, ok in ZDR_COMPLIANT.items() if ok] == [ALLOWED]


# ---------------------------------------------------------------------------
# 4-6. Titles, summarizer, web search
# ---------------------------------------------------------------------------

def test_titles_make_no_model_call_under_zdr(monkeypatch):
    _zdr_on()
    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI",
                        lambda *a, **k: pytest.fail("built a title model on a ZDR box"))
    from app.services.thread_service import generate_title_with_llm

    title = asyncio.run(generate_title_with_llm("Help me build a gradebook for my class"))
    assert title == "Help me build a gradebook for my class"


def _summarizer_labels():
    from app.agents.leonardo.summarization import (
        _ACTIVE_CHAT_MODEL,
        make_summarization_middleware,
    )

    mw = make_summarization_middleware(summary_prompt="x {messages}")
    token = _ACTIVE_CHAT_MODEL.set("gpt-5-mini")
    try:
        return [label for label, _ in mw._summarizer_candidates()]
    finally:
        _ACTIVE_CHAT_MODEL.reset(token)


def test_summarizer_uses_the_chat_model_only_under_zdr():
    _zdr_on()
    assert _summarizer_labels() == ["chat model gpt-5-mini"]


def test_summarizer_keeps_its_fallback_chain_without_zdr():
    assert _summarizer_labels() == ["chat model gpt-5-mini", "fallback chain"]


def test_summarization_model_is_the_allowed_model_under_zdr():
    _zdr_on()
    model, _, _ = llm_factory.make_summarization_model()
    assert FIREWORKS_HOST in str(getattr(model, "api_base", "") or getattr(model, "openai_api_base", ""))


def test_web_search_is_disabled_under_zdr(monkeypatch):
    _zdr_on()
    from app.agents.leonardo.rails_agent import tools

    monkeypatch.setattr(tools, "get_tavily_client",
                        lambda: pytest.fail("called Tavily on a ZDR box"))
    assert tools.internet_search.invoke({"query": "ferpa"}) == zdr.WEB_SEARCH_DISABLED_MESSAGE


# ---------------------------------------------------------------------------
# 7. Tracing and analytics
# ---------------------------------------------------------------------------

def test_langsmith_is_forced_off_under_zdr_whatever_the_env_says(monkeypatch):
    from langsmith import utils as ls_utils

    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    ls_utils.get_env_var.cache_clear()
    assert zdr.tracing_enabled(), "precondition: env turns tracing on"

    _zdr_on()
    zdr.zdr_state()
    assert not zdr.tracing_enabled()


def test_posthog_key_is_not_rendered_under_zdr(monkeypatch):
    monkeypatch.setenv("LLAMABOT_POSTHOG_KEY", "phc_real")
    from app.routers.ui import _posthog_config

    assert _posthog_config()[0] == "phc_real"
    _zdr_on()
    assert _posthog_config() == ("", "")


# ---------------------------------------------------------------------------
# 8. Agents that build a fixed vendor client are unreachable under ZDR
# ---------------------------------------------------------------------------

def test_llamabot_agent_refuses_under_zdr(monkeypatch):
    _zdr_on()
    from app.agents.llamabot import nodes

    monkeypatch.setattr(nodes, "_llm_instance", None)  # any use would crash differently
    with pytest.raises(zdr.ZDRRefused):
        nodes.leo({"messages": [], "agent_prompt": ""})


def test_html_agent_refuses_under_zdr():
    _zdr_on()
    from app.agents.llamapress import html_agent

    with pytest.raises(zdr.ZDRRefused):
        html_agent.write_html_page_agent({"messages": []})


# ---------------------------------------------------------------------------
# 9. Report it
# ---------------------------------------------------------------------------

def test_report_health_payload_carries_the_zdr_attestation(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services.mothership_client import MothershipClient

    _zdr_on()
    client = MothershipClient.__new__(MothershipClient)
    client.config = {"mothership_url": "https://m.example.com", "instance_name": "mbc",
                     "mothership_api_token": "tok"}
    monkeypatch.delenv("LLAMABOT_TELEMETRY_DISABLED", raising=False)
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        resp = MagicMock()
        resp.json.return_value = {}
        return resp

    ctx = AsyncMock()
    ctx.__aenter__.return_value.post = fake_post
    with patch("httpx.AsyncClient", return_value=ctx):
        asyncio.run(client.report_rails_health(rails_status=200, rails_ms=5))

    assert captured["payload"]["zdr"] == {
        "enforced": True,
        "allowed_models": [ALLOWED],
        "compliant_models": [ALLOWED],
        "tracing": "off",
        "web_search": "off",
    }


# ---------------------------------------------------------------------------
# Addendum: the picker and the ChatGPT connect flow
# ---------------------------------------------------------------------------

def test_chatgpt_connect_routes_refuse_under_zdr():
    from fastapi import HTTPException

    from app.routers import chatgpt_auth as routes

    _zdr_on()
    for call in (lambda: routes.start(user=object()),
                 lambda: routes.poll(body=None, user=object(), session=None),
                 lambda: routes.import_credential(body=None, user=object(), session=None)):
        with pytest.raises(HTTPException) as exc:
            result = call()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_available_models_flags_policy_disabled_chatgpt_models(async_client):
    _zdr_on()
    body = (await async_client.get("/api/available-models")).json()
    by_value = {m["value"]: m for m in body["models"]}
    for name in ("gpt-6-luna-chatgpt", "gpt-6-sol-chatgpt", "gpt-5-mini"):
        assert by_value[name]["available"] is False
        assert by_value[name]["disabled_by_policy"] is True
    assert by_value[ALLOWED]["available"] is True
    assert body["zdr"] is True


# ---------------------------------------------------------------------------
# The one that matters
# ---------------------------------------------------------------------------

# tiktoken fetches its public encoding file on first use when its cache is
# empty: a plain GET for a static file that carries no customer data. Not a
# vendor that receives prompts, so it is the one other host allowed here.
_STATIC_DOWNLOAD_HOSTS = {"openaipublic.blob.core.windows.net"}


class _HostRecorder:
    """Records every outbound HTTP host and fails the request without sending it."""

    def __init__(self, monkeypatch):
        self.hosts = []
        recorder = self

        def sync_send(client, request, *a, **k):
            recorder.hosts.append(request.url.host)
            raise httpx.ConnectError("blocked by test", request=request)

        async def async_send(client, request, *a, **k):
            recorder.hosts.append(request.url.host)
            raise httpx.ConnectError("blocked by test", request=request)

        def requests_send(session, request, **k):
            recorder.hosts.append(httpx.URL(request.url).host)
            raise requests.ConnectionError("blocked by test")

        monkeypatch.setattr(httpx.Client, "send", sync_send)
        monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
        monkeypatch.setattr(requests.Session, "send", requests_send)


def _exercise_every_path(monkeypatch):
    """A title, a summarizer whose chat model fails, get_llm for every model, web search."""
    from app.agents.leonardo.rails_agent import tools
    from app.agents.leonardo.summarization import (
        _ACTIVE_CHAT_MODEL,
        make_summarization_middleware,
    )
    from app.services.thread_service import generate_title_with_llm

    monkeypatch.setattr(tools, "_tavily_client", None)
    built = []
    real_build = llm_factory._build_client
    monkeypatch.setattr(llm_factory, "_build_client",
                        lambda name: built.append(name) or real_build(name))

    asyncio.run(generate_title_with_llm("Help me build a gradebook for my class"))

    mw = make_summarization_middleware(summary_prompt="Summarize {messages}")
    token = _ACTIVE_CHAT_MODEL.set("gpt-5-mini")
    try:
        from langchain_core.messages import HumanMessage
        mw._create_summary([HumanMessage(content="student record 123")])
    finally:
        _ACTIVE_CHAT_MODEL.reset(token)

    for name in sorted(MODEL_CAPABILITIES):
        try:
            llm_factory.get_llm(name).invoke("hi")
        except Exception:
            pass

    tools.internet_search.invoke({"query": "student 123"})
    return built


def test_zdr_box_contacts_only_allowed_vendor(monkeypatch):
    _zdr_on()
    recorder = _HostRecorder(monkeypatch)

    built = _exercise_every_path(monkeypatch)

    assert set(built) == {ALLOWED}
    assert recorder.hosts, "nothing was sent at all; the recorder is not wired"
    assert set(recorder.hosts) - _STATIC_DOWNLOAD_HOSTS == {FIREWORKS_HOST}
    assert not zdr.tracing_enabled()


def test_non_zdr_box_behaviour_is_unchanged(monkeypatch):
    """The ~200 non-ZDR boxes: titles still use OpenAI, search still uses Tavily."""
    _push({"enabled_models": sorted(MODEL_CAPABILITIES)})
    recorder = _HostRecorder(monkeypatch)

    built = _exercise_every_path(monkeypatch)

    assert "api.openai.com" in recorder.hosts
    assert "api.tavily.com" in recorder.hosts
    assert len(set(built)) > 1
