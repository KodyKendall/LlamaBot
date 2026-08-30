"""The config-driven OpenRouter model registry (0.7.5).

Every other model costs six code edits to add. OpenRouter fronts one
OpenAI-compatible endpoint over hundreds of models and dozens of provider
endpoints each (``deepseek/deepseek-v4-flash-0731`` alone has 28), so trying one
is a pricing experiment rather than a code change — these entries live in a
host-mounted JSON file instead.

What has to hold for that to be safe:

  * a malformed or half-written config can never take the dropdown down,
  * an entry can never silently become something other than what it says (a
    missing provider pin means OpenRouter load-balances across all 28 endpoints,
    which is exactly what pinning relace/fp4 exists to prevent),
  * and a registered model is subject to the same operator policy as any other.
"""
import json

import pytest

from app.agents.leonardo import model_policy, openrouter_models as registry
from app.agents.leonardo.llm_factory import ChatDeepSeekWithReasoning, get_llm
from app.agents.leonardo.model_capabilities import get_model_capabilities

RELACE = "deepseek-flash-0731-relace"
DIGITALOCEAN = "deepseek-flash-0731-digitalocean"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """No overlay, no policy config, a key present — the baseline box."""
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    monkeypatch.setenv("OPENROUTER_MODELS_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    for var in ("ENABLED_MODELS", "DISABLED_MODELS", "OPENROUTER_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def _overlay(monkeypatch, tmp_path, payload):
    path = tmp_path / "openrouter_models.json"
    path.write_text(json.dumps(payload) if isinstance(payload, dict) else payload)
    monkeypatch.setenv("OPENROUTER_MODELS_CONFIG", str(path))
    return path


# --- the compiled-in entry -------------------------------------------------

def test_relace_entry_is_registered_out_of_the_box():
    assert RELACE in registry.openrouter_models()


def test_relace_pins_one_provider_with_fallbacks_off():
    """The whole economic point. Without the pin OpenRouter load-balances across
    all 28 endpoints for this model, so the $0.04/$0.08 per-M rate that justifies
    the entry evaporates on the first silent reroute — and prompt caching, which
    is per-replica, stops hitting too."""
    entry = registry.get_openrouter_model(RELACE)
    provider = entry["extra_body"]["provider"]
    assert provider["order"] == ["relace/fp4"]
    assert provider["allow_fallbacks"] is False


def test_digitalocean_entry_is_registered_out_of_the_box():
    assert DIGITALOCEAN in registry.openrouter_models()


def test_digitalocean_pins_its_own_endpoint_with_fallbacks_off():
    provider = registry.get_openrouter_model(DIGITALOCEAN)["extra_body"]["provider"]
    assert provider["order"] == ["digitalocean"]
    assert provider["allow_fallbacks"] is False


def test_the_two_0731_entries_are_siblings_not_a_repoint():
    """Same weights, different host, and BOTH offered — who serves (and bills
    for) a turn stays an explicit user choice, exactly like the RunPod vs
    Fireworks Nemotron pair. A second entry must never quietly replace the first."""
    models = registry.openrouter_models()
    assert {RELACE, DIGITALOCEAN} <= set(models)
    assert models[RELACE]["model"] == models[DIGITALOCEAN]["model"]
    assert (
        models[RELACE]["extra_body"]["provider"]["order"]
        != models[DIGITALOCEAN]["extra_body"]["provider"]["order"]
    )


def test_every_compiled_in_entry_pins_a_provider():
    """A base entry without a pin would silently load-balance across all 28
    endpoints for its model — the exact failure the registry exists to prevent,
    and invisible until a bill arrives."""
    for name, entry in registry.openrouter_models().items():
        provider = entry.get("extra_body", {}).get("provider")
        assert provider and provider.get("order"), f"{name} does not pin an endpoint"


def test_relace_targets_the_0731_model_id():
    assert registry.get_openrouter_model(RELACE)["model"] == "deepseek/deepseek-v4-flash-0731"


# --- overlay layering ------------------------------------------------------

def test_overlay_entry_is_registered(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": {"cheap-thing": {
        "label": "Cheap Thing", "model": "vendor/cheap-thing",
    }}})
    assert "cheap-thing" in registry.openrouter_models()


def test_overlay_wins_over_the_compiled_entry(monkeypatch, tmp_path):
    """A box must be able to re-point or re-price a built-in entry without a
    rebuild — that is the difference between config and a hardcoded default."""
    _overlay(monkeypatch, tmp_path, {"models": {RELACE: {
        "model": "deepseek/deepseek-v4-flash-0731",
        "provider": {"order": ["open-inference/fp4"], "allow_fallbacks": True},
    }}})
    provider = registry.get_openrouter_model(RELACE)["extra_body"]["provider"]
    assert provider["order"] == ["open-inference/fp4"]
    assert provider["allow_fallbacks"] is True


def test_registry_is_not_cached_between_reads(monkeypatch, tmp_path):
    """An operator edits the mounted file live; caching would mean a container
    restart just to try a different provider endpoint."""
    _overlay(monkeypatch, tmp_path, {"models": {"a": {"model": "v/a"}}})
    assert "a" in registry.openrouter_models()
    _overlay(monkeypatch, tmp_path, {"models": {"b": {"model": "v/b"}}})
    assert "b" in registry.openrouter_models()


# --- fail-open -------------------------------------------------------------

def test_missing_overlay_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_MODELS_CONFIG", str(tmp_path / "nope.json"))
    assert RELACE in registry.openrouter_models()


def test_malformed_json_falls_back_to_the_base(monkeypatch, tmp_path):
    """A half-written config file must never take the dropdown down."""
    _overlay(monkeypatch, tmp_path, '{"models": {"broken": ')
    models = registry.openrouter_models()
    assert RELACE in models
    assert "broken" not in models


def test_models_key_of_the_wrong_type_is_ignored(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": ["not", "an", "object"]})
    assert RELACE in registry.openrouter_models()


def test_entry_without_a_model_id_is_dropped(monkeypatch, tmp_path):
    """It cannot build a client, so registering it would put a dead option in the
    dropdown that silently falls back to the box default when picked."""
    _overlay(monkeypatch, tmp_path, {"models": {
        "no-id": {"label": "No Model Id"},
        "fine": {"model": "vendor/fine"},
    }})
    models = registry.openrouter_models()
    assert "no-id" not in models
    assert "fine" in models, "one bad entry must not discard its neighbours"


def test_entry_that_is_not_an_object_is_dropped(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": {"junk": "just a string"}})
    assert "junk" not in registry.openrouter_models()


# --- normalization ---------------------------------------------------------

def test_capabilities_default_to_text_only(monkeypatch, tmp_path):
    """The OPPOSITE of get_model_capabilities' permissive unknown-model default,
    on purpose: this is a file the operator just wrote, and guessing "probably
    multimodal" would offer an image upload that 400s at the provider."""
    _overlay(monkeypatch, tmp_path, {"models": {"plain": {"model": "vendor/plain"}}})
    assert get_model_capabilities("plain") == {"images": False, "video": False, "pdf": False}


def test_declared_capabilities_are_honoured(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": {"seer": {
        "model": "vendor/seer", "capabilities": {"images": True},
    }}})
    assert get_model_capabilities("seer") == {"images": True, "video": False, "pdf": False}


def test_capability_values_are_coerced_to_bool(monkeypatch, tmp_path):
    """A JSON string would make `if caps['images']` accidentally true."""
    _overlay(monkeypatch, tmp_path, {"models": {"sneaky": {
        "model": "vendor/sneaky", "capabilities": {"images": "false"},
    }}})
    assert get_model_capabilities("sneaky")["images"] is True  # non-empty string is truthy
    _overlay(monkeypatch, tmp_path, {"models": {"sneaky": {
        "model": "vendor/sneaky", "capabilities": {"images": ""},
    }}})
    assert get_model_capabilities("sneaky")["images"] is False


def test_label_defaults_to_the_model_name(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": {"bare": {"model": "vendor/bare"}}})
    entry = registry.get_openrouter_model("bare")
    assert entry["label"] == "bare"
    assert entry["short_label"] == "bare"


def test_enabled_false_parks_an_entry(monkeypatch, tmp_path):
    """Lets an operator keep a config block (and its pricing notes) without the
    model reaching the dropdown."""
    _overlay(monkeypatch, tmp_path, {"models": {"parked": {
        "model": "vendor/parked", "enabled": False,
    }}})
    assert "parked" not in registry.openrouter_models()


def test_extra_body_passes_through_with_provider_authoritative(monkeypatch, tmp_path):
    """`extra_body` is the escape hatch for any other OpenRouter request field;
    an explicit `provider` block must still win for the common case."""
    _overlay(monkeypatch, tmp_path, {"models": {"x": {
        "model": "vendor/x",
        "extra_body": {"transforms": ["middle-out"], "provider": {"order": ["ignored"]}},
        "provider": {"order": ["kept/fp8"]},
    }}})
    extra = registry.get_openrouter_model("x")["extra_body"]
    assert extra["transforms"] == ["middle-out"]
    assert extra["provider"]["order"] == ["kept/fp8"]


# --- client construction ---------------------------------------------------

@pytest.mark.parametrize("name", [RELACE, DIGITALOCEAN])
def test_get_llm_routes_a_registry_model_to_openrouter(name):
    llm = get_llm(name)
    assert llm.api_base == "https://openrouter.ai/api/v1"
    assert llm.model_name == "deepseek/deepseek-v4-flash-0731"


def test_the_digitalocean_pin_reaches_the_request_body():
    assert get_llm(DIGITALOCEAN).extra_body == {
        "provider": {"order": ["digitalocean"], "allow_fallbacks": False}
    }


def test_the_provider_pin_reaches_the_request_body():
    """extra_body is what the OpenAI-compatible client passes through untouched;
    OpenRouter reads `provider` as a top-level request field. If this stops
    threading through, the pin silently becomes a no-op and billing moves."""
    assert get_llm(RELACE).extra_body == {
        "provider": {"order": ["relace/fp4"], "allow_fallbacks": False}
    }


def test_the_underlying_sdk_client_really_points_at_openrouter():
    """The LangChain field can read None while the lazily-built SDK client is the
    thing that carries the URL — same trap as the api-key leak tests."""
    assert "openrouter.ai" in str(get_llm(RELACE).client._client.base_url)


def test_reasoning_entries_get_the_reasoning_client():
    assert isinstance(get_llm(RELACE), ChatDeepSeekWithReasoning)


def test_reasoning_false_gets_a_plain_client(monkeypatch, tmp_path):
    """OpenRouter normalizes thinking differently from DeepSeek's own API, so the
    client choice stays per-entry rather than sniffed from the model id."""
    from langchain_openai import ChatOpenAI

    _overlay(monkeypatch, tmp_path, {"models": {"plainmodel": {
        "model": "vendor/plain", "reasoning": False,
    }}})
    llm = get_llm("plainmodel")
    assert isinstance(llm, ChatOpenAI)
    assert not isinstance(llm, ChatDeepSeekWithReasoning)


def test_base_url_is_overridable(monkeypatch):
    """So a box can point at a proxy — or a test at a mock — without code."""
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://openrouter.invalid/v1")
    assert get_llm(RELACE).api_base == "http://openrouter.invalid/v1"


def test_sdk_retries_stay_off_like_every_other_model():
    """Provider retries are opaque and multiply the 180s timeout; middleware owns
    classified retries instead."""
    llm = get_llm(RELACE)
    assert llm.max_retries == 0
    assert llm.request_timeout == 180


# --- policy ----------------------------------------------------------------

def test_registering_a_model_enables_it(monkeypatch, tmp_path):
    """The registry file is operator-owned and host-mounted, like instance.json.
    Requiring ENABLED_MODELS too would mean configuring one intent twice."""
    _overlay(monkeypatch, tmp_path, {"models": {"newthing": {"model": "vendor/newthing"}}})
    assert model_policy.is_model_enabled("newthing") is True


def test_an_explicit_disable_still_wins(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": {"newthing": {"model": "vendor/newthing"}}})
    monkeypatch.setenv("DISABLED_MODELS", "newthing")
    assert model_policy.is_model_enabled("newthing") is False


def test_an_allowlist_that_omits_it_does_not_disable_it(monkeypatch, tmp_path):
    """Step 2b sits above the allow-list, same as the other fail-open rules."""
    _overlay(monkeypatch, tmp_path, {"models": {"newthing": {"model": "vendor/newthing"}}})
    monkeypatch.setenv("ENABLED_MODELS", "deepseek-v4-flash")
    assert model_policy.is_model_enabled("newthing") is True


def test_registry_models_are_known_to_the_fallback_walk(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": {"newthing": {"model": "vendor/newthing"}}})
    known = model_policy.known_models()
    assert "newthing" in known
    # ...but LAST: a third-party-routed endpoint must never outrank a first-party
    # model when picking what the box runs by default.
    assert known.index("newthing") > known.index("deepseek-v4-flash")


def test_a_registry_model_is_never_the_fleet_default():
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL, FALLBACK_TEXT_MODEL

    assert RELACE not in (DEFAULT_LLM_MODEL, FALLBACK_TEXT_MODEL)
    assert DIGITALOCEAN not in (DEFAULT_LLM_MODEL, FALLBACK_TEXT_MODEL)


def test_a_registry_model_is_never_the_lockout_fallback(monkeypatch, tmp_path):
    """enabled_default_model walks known_models() when everything else is off, and
    step 2b enables registry entries wherever a key exists — so without a guard
    the fallback quietly moves the box onto a third-party-routed endpoint someone
    added to try. A registered model is a thing a user PICKS, never what a box
    falls back to running."""
    _overlay(monkeypatch, tmp_path, {"models": {"newthing": {"model": "vendor/newthing"}}})
    monkeypatch.setenv("DISABLED_MODELS", ",".join(model_policy._KNOWN_MODELS))
    resolved = model_policy.enabled_default_model()
    assert resolved != "newthing"
    assert resolved != RELACE


def test_a_keyless_box_is_not_offered_registry_models(monkeypatch, tmp_path):
    """The registry ships a compiled-in example entry. Without the key check, every
    fleet box would grow a dropdown option it cannot run — breaking the invariant
    that an unconfigured box offers exactly the two blessed models."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _overlay(monkeypatch, tmp_path, {"models": {"newthing": {"model": "vendor/newthing"}}})
    assert model_policy.is_model_enabled("newthing") is False
    assert model_policy.is_model_enabled(RELACE) is False
    assert model_policy.is_model_enabled(DIGITALOCEAN) is False


def test_switching_lock_still_pins_the_default(monkeypatch, tmp_path):
    """A registered model must not punch through the coarse operator lock —
    step 1a is above step 2b."""
    _overlay(monkeypatch, tmp_path, {"models": {"newthing": {"model": "vendor/newthing"}}})
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "false")
    assert model_policy.is_model_enabled("newthing") is False


# --- the endpoint contract the dropdown depends on -------------------------

@pytest.mark.asyncio
async def test_available_models_includes_registry_models(async_client):
    """Registry models are not in the hand-maintained model_api_keys map, so they
    have to be folded into the response separately — otherwise the dropdown never
    hears about them and the whole config path is inert."""
    response = await async_client.get("/api/available-models")
    assert response.status_code == 200
    by_value = {m["value"]: m for m in response.json()["models"]}

    assert RELACE in by_value, "the registry entry never reached /api/available-models"
    entry = by_value[RELACE]
    assert entry["capabilities"] == {"images": False, "video": False, "pdf": False}


@pytest.mark.asyncio
async def test_registry_models_ship_a_label_for_the_dropdown(async_client):
    """chat.html has no <option> for a config-driven model — it cannot know the
    name — so the frontend builds one from these fields."""
    by_value = {m["value"]: m for m in (await async_client.get("/api/available-models")).json()["models"]}

    assert by_value[RELACE]["label"] == "DeepSeek V4 Flash 0731 (Relace fp4)"
    assert by_value[RELACE]["short_label"] == "DS 0731 Relace"


@pytest.mark.asyncio
async def test_hardcoded_models_do_not_ship_a_label(async_client):
    """`label` is the signal "create an option for me". Sending it for a model
    that already has markup would let a runtime response paper over a genuine
    frontend/backend skew instead of surfacing it."""
    by_value = {m["value"]: m for m in (await async_client.get("/api/available-models")).json()["models"]}

    assert "label" not in by_value["deepseek-v4-flash"]
