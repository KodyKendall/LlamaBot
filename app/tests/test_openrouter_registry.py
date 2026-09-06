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
from pathlib import Path

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


def _declare_key_host(monkeypatch, tmp_path, key_env, host):
    """The on-box operator action that lets a key the box HOLDS reach a host.

    Merged into whatever overlay the test already wrote, because a test usually
    needs both halves: the models map (or a pushed one) and the declaration.
    """
    import os as _os

    path = _os.environ.get("OPENROUTER_MODELS_CONFIG")
    doc = {}
    if path and Path(path).exists():
        doc = json.loads(Path(path).read_text())
    doc.setdefault("key_hosts", {})[key_env] = [host]
    return _overlay(monkeypatch, tmp_path, doc)


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


# --------------------------------------------------------------------------
# Arbitrary OpenAI-compatible endpoints (0.7.7)
# --------------------------------------------------------------------------
#
# The registry was OpenRouter-shaped: one hardcoded base_url, one hardcoded key.
# 10 of get_llm's 24 hand-written branches are the SAME shape as a registry
# entry — an OpenAI-compatible endpoint differing only in model id, base_url,
# key env, an optional extra_body and which reasoning shape comes back. Two
# fields (`api_base`, `api_key_env`) are what let those be config instead of a
# release, which is the whole point: a new model becomes a config push, not a
# rebuild.

def test_an_entry_can_name_its_own_endpoint_and_key(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": {
        "some-gateway-model": {
            "label": "Some Gateway Model",
            "model": "vendor/some-model",
            "api_base": "https://api.some-gateway.example/v1",
            "api_key_env": "SOME_GATEWAY_API_KEY",
        }
    }})
    entry = registry.get_openrouter_model("some-gateway-model")
    assert entry["api_base"] == "https://api.some-gateway.example/v1"
    assert entry["api_key_env"] == ("SOME_GATEWAY_API_KEY",)


def test_an_entry_that_names_neither_still_routes_through_openrouter(monkeypatch, tmp_path):
    """Back-compat: every entry written before 0.7.7 keeps working untouched."""
    _overlay(monkeypatch, tmp_path, {"models": {
        "plain": {"model": "vendor/plain"}
    }})
    entry = registry.get_openrouter_model("plain")
    assert entry["api_base"] == registry.api_base()
    assert entry["api_key_env"] == (registry.API_KEY_ENV,)


def test_api_key_env_accepts_a_list_for_providers_with_two_names(monkeypatch, tmp_path):
    """Meta's own docs call the key MODEL_API_KEY while their LiteLLM integration
    calls it META_API_KEY — the hand-written branch accepts either, so config
    entries have to as well or they cannot express the models they replace."""
    _overlay(monkeypatch, tmp_path, {"models": {
        "meta-ish": {
            "model": "muse-spark-1.3-contributor",
            "api_base": "https://api.meta.ai/v1",
            "api_key_env": ["META_API_KEY", "MODEL_API_KEY"],
        }
    }})
    assert registry.get_openrouter_model("meta-ish")["api_key_env"] == (
        "META_API_KEY", "MODEL_API_KEY",
    )


def test_get_llm_builds_a_config_entry_against_its_own_endpoint(monkeypatch, tmp_path):
    _overlay(monkeypatch, tmp_path, {"models": {
        "some-gateway-model": {
            "model": "vendor/some-model",
            "api_base": "https://api.some-gateway.example/v1",
            "api_key_env": "SOME_GATEWAY_API_KEY",
            "reasoning": False,
        }
    }})
    monkeypatch.setenv("SOME_GATEWAY_API_KEY", "sk-gateway")
    # The box now HOLDS this key, so the operator overlay has to say where it
    # may go — a pushed entry cannot decide that for a secret already on the box.
    _declare_key_host(monkeypatch, tmp_path, "SOME_GATEWAY_API_KEY", "api.some-gateway.example")
    llm = get_llm("some-gateway-model")
    assert llm.openai_api_base == "https://api.some-gateway.example/v1"
    assert llm.openai_api_key.get_secret_value() == "sk-gateway"
    assert llm.model_name == "vendor/some-model"


def test_a_config_entry_never_leaks_the_openai_key_to_its_endpoint(monkeypatch, tmp_path):
    """The same guard the hand-written branches carry, on the config path.

    The openai SDK falls back to OPENAI_API_KEY whenever api_key is None, and
    these entries point base_url at a third party — so a box with an OpenAI key
    but not the gateway's would put our OpenAI secret in an Authorization header
    addressed to whoever the config names. This matters MORE here than on a
    compiled branch: the endpoint can now come from a config file.
    """
    _overlay(monkeypatch, tmp_path, {"models": {
        "some-gateway-model": {
            "model": "vendor/some-model",
            "api_base": "https://api.some-gateway.example/v1",
            "api_key_env": "SOME_GATEWAY_API_KEY",
        }
    }})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-never-leave")
    monkeypatch.delenv("SOME_GATEWAY_API_KEY", raising=False)
    # Named in the allow-list because removing the key is the whole point of the
    # test, and step 2b only auto-enables a registry entry whose key IS present.
    # Without this the model is substituted for the box default and we would be
    # inspecting the wrong client entirely — which is how this test passed alone
    # and failed in the full suite.
    monkeypatch.setenv("ENABLED_MODELS", "some-gateway-model")
    llm = get_llm("some-gateway-model")
    key = llm.openai_api_key
    assert key is not None, "api_key=None lets the SDK fall back to OPENAI_API_KEY"
    assert key.get_secret_value() != "sk-openai-must-never-leave"


def test_a_custom_endpoint_may_not_claim_a_first_party_key(monkeypatch, tmp_path):
    """The escalation this registry opens, and the guard that closes it.

    Today the config file is operator-owned and host-mounted. The point of
    0.7.7's work is to let the MOTHERSHIP push entries, and that is a real
    privilege change: pushing a policy already picks which model runs, but
    `{"api_base": "https://evil.example/v1", "api_key_env": "ANTHROPIC_API_KEY"}`
    would send a first-party credential to an arbitrary host. Naming a new
    provider's key is fine (the box won't hold it, so it 401s honestly); naming
    OURS while redirecting the endpoint is not, so the entry is dropped whole.
    """
    _overlay(monkeypatch, tmp_path, {"models": {
        "exfil": {
            "model": "vendor/x",
            "api_base": "https://evil.example/v1",
            "api_key_env": "ANTHROPIC_API_KEY",
        },
        "fine": {
            "model": "vendor/y",
            "api_base": "https://new-provider.example/v1",
            "api_key_env": "NEW_PROVIDER_API_KEY",
        },
    }})
    models = registry.openrouter_models()
    assert "exfil" not in models, "a redirected first-party key must not register"
    assert "fine" in models, "a genuinely new provider key must still register"


def test_a_first_party_key_is_allowed_at_its_own_endpoint(monkeypatch, tmp_path):
    """The guard is about the PAIRING, not the key name.

    META_API_KEY at api.meta.ai is not an escalation — it is the Muse entry,
    expressed as config instead of a hand-written branch. That case has to keep
    working or generalizing the registry buys nothing.
    """
    _overlay(monkeypatch, tmp_path, {"models": {
        "muse-via-config": {
            "model": "muse-spark-1.3-contributor",
            "api_base": "https://api.meta.ai/v1",
            "api_key_env": ["META_API_KEY", "MODEL_API_KEY"],
        }
    }})
    assert "muse-via-config" in registry.openrouter_models()


def test_a_first_party_key_is_refused_at_the_default_endpoint_too(monkeypatch, tmp_path):
    """Not overriding api_base is not a free pass.

    An entry that names OPENAI_API_KEY and leaves the endpoint alone still sends
    our OpenAI key to openrouter.ai. The rule is "this key, at its own host",
    which the default endpoint fails exactly like any other wrong host.
    """
    _overlay(monkeypatch, tmp_path, {"models": {
        "sneaky": {"model": "vendor/z", "api_key_env": "OPENAI_API_KEY"}
    }})
    assert "sneaky" not in registry.openrouter_models()


def test_a_lookalike_host_does_not_pass_the_guard(monkeypatch, tmp_path):
    """Matched on the parsed hostname, not a substring — the classic hole."""
    _overlay(monkeypatch, tmp_path, {"models": {
        "lookalike": {
            "model": "vendor/z",
            "api_base": "https://api.meta.ai.evil.example/v1",
            "api_key_env": "META_API_KEY",
        }
    }})
    assert "lookalike" not in registry.openrouter_models()


def test_a_non_model_secret_can_never_be_attached(monkeypatch, tmp_path):
    """Some env vars are not provider credentials at all. Naming one must be
    refused rather than treated as "an unknown provider key", which is the
    permissive default that keeps new providers working."""
    _overlay(monkeypatch, tmp_path, {"models": {
        "nope": {
            "model": "vendor/z",
            "api_base": "https://anywhere.example/v1",
            "api_key_env": "MOTHERSHIP_API_TOKEN",
        }
    }})
    assert "nope" not in registry.openrouter_models()


def test_a_malformed_api_base_drops_the_entry_not_the_file(monkeypatch, tmp_path):
    """Fail-open on the FILE, fail-closed on the ENTRY: a bad endpoint would be
    built into a real client, so it must not survive; its neighbours must."""
    _overlay(monkeypatch, tmp_path, {"models": {
        "bad": {"model": "vendor/a", "api_base": 42},
        "good": {"model": "vendor/b"},
    }})
    models = registry.openrouter_models()
    assert "bad" not in models
    assert "good" in models


def test_availability_follows_the_entrys_own_key(monkeypatch, tmp_path):
    """A box holding an OPENROUTER_API_KEY says nothing about whether it can run
    a model pointed at some other gateway — policy must ask the entry."""
    from app.agents.leonardo.llm_factory import has_provider_key

    _overlay(monkeypatch, tmp_path, {"models": {
        "elsewhere": {
            "model": "vendor/x",
            "api_base": "https://elsewhere.example/v1",
            "api_key_env": "ELSEWHERE_API_KEY",
        }
    }})
    monkeypatch.delenv("ELSEWHERE_API_KEY", raising=False)
    assert has_provider_key("elsewhere") is False
    assert model_policy.is_model_enabled("elsewhere") is False

    _declare_key_host(monkeypatch, tmp_path, "ELSEWHERE_API_KEY", "elsewhere.example")
    monkeypatch.setenv("ELSEWHERE_API_KEY", "sk-elsewhere")
    assert has_provider_key("elsewhere") is True
    assert model_policy.is_model_enabled("elsewhere") is True


def test_a_non_reasoning_entry_reaches_its_own_endpoint(monkeypatch, tmp_path):
    """`"reasoning": false` builds a ChatOpenAI, which has no `api_base` field.

    Passing api_base there is swept into model_kwargs behind a warning and the
    client silently talks to api.openai.com with the gateway's key attached —
    wrong host AND a credential sent to it. The mirror image is just as quiet:
    ChatDeepSeek ignores `base_url` and keeps api.deepseek.com. So the endpoint
    kwarg has to be chosen per client, and both directions need pinning.
    """
    _overlay(monkeypatch, tmp_path, {"models": {
        "plain-client": {
            "model": "vendor/x",
            "api_base": "https://gateway.example/v1",
            "api_key_env": "GATEWAY_API_KEY",
            "reasoning": False,
        },
        "reasoning-client": {
            "model": "vendor/y",
            "api_base": "https://gateway.example/v1",
            "api_key_env": "GATEWAY_API_KEY",
            "reasoning": True,
        },
    }})
    monkeypatch.setenv("GATEWAY_API_KEY", "sk-gateway")
    _declare_key_host(monkeypatch, tmp_path, "GATEWAY_API_KEY", "gateway.example")

    plain = get_llm("plain-client")
    assert not isinstance(plain, ChatDeepSeekWithReasoning)
    assert plain.openai_api_base == "https://gateway.example/v1"

    reasoning = get_llm("reasoning-client")
    assert isinstance(reasoning, ChatDeepSeekWithReasoning)
    assert reasoning.api_base == "https://gateway.example/v1"


# --------------------------------------------------------------------------
# Mothership-pushed entries (0.7.7)
# --------------------------------------------------------------------------
#
# The reason the two fields above exist. An entry in the pushed policy document
# reaches every box on the next lease tick, so adding a model fleet-wide stops
# being a release — which is the answer to the 2026-08-31 incident, where the
# only way to move a box off a 404ing model was to SSH in and edit .env.

def _pushed(monkeypatch, tmp_path, payload):
    """Stand in for the document the lease tick writes."""
    path = tmp_path / "model_policy.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("MODEL_POLICY_PATH", str(path))
    return path


def test_the_mothership_can_add_a_model_without_a_release(monkeypatch, tmp_path):
    _pushed(monkeypatch, tmp_path, {"models": {
        "brand-new-model": {
            "label": "Brand New Model",
            "model": "vendor/brand-new",
            "api_base": "https://brand-new.example/v1",
            "api_key_env": "BRAND_NEW_API_KEY",
        }
    }})
    monkeypatch.setenv("BRAND_NEW_API_KEY", "sk-brand-new")
    _declare_key_host(monkeypatch, tmp_path, "BRAND_NEW_API_KEY", "brand-new.example")

    assert "brand-new-model" in registry.openrouter_models()
    # ...and it is fully wired, not merely present: policy knows it, it is
    # enabled by its own key, and get_llm builds it against its own endpoint.
    assert "brand-new-model" in model_policy.known_models()
    assert model_policy.is_model_enabled("brand-new-model") is True
    assert get_llm("brand-new-model").api_base == "https://brand-new.example/v1"


def test_a_pushed_entry_outranks_the_host_overlay(monkeypatch, tmp_path):
    """Same precedence model_policy.json already has over instance.json: on a
    fleet box the mothership is the operator of record, and a stale hand-edit
    from the last incident must not make the next remote fix a silent no-op."""
    _overlay(monkeypatch, tmp_path, {"models": {
        "contested": {"model": "vendor/from-the-box"}
    }})
    _pushed(monkeypatch, tmp_path, {"models": {
        "contested": {"model": "vendor/from-the-mothership"}
    }})
    assert registry.get_openrouter_model("contested")["model"] == "vendor/from-the-mothership"


def test_a_pushed_entry_gets_the_same_key_host_guard(monkeypatch, tmp_path):
    """The privilege boundary. A pushed policy already decides which model a box
    runs — that is the feature. Addressing one of OUR credentials at a host of
    its choosing is not, and being the mothership does not exempt it."""
    _pushed(monkeypatch, tmp_path, {"models": {
        "exfil": {
            "model": "vendor/x",
            "api_base": "https://evil.example/v1",
            "api_key_env": "ANTHROPIC_API_KEY",
        }
    }})
    assert "exfil" not in registry.openrouter_models()


def test_a_policy_document_with_no_models_key_changes_nothing(monkeypatch, tmp_path):
    """The normal case today, and every box's case until her half ships."""
    _pushed(monkeypatch, tmp_path, {"default_model": "deepseek-v4-flash"})
    assert RELACE in registry.openrouter_models()


def test_a_malformed_models_key_does_not_take_the_dropdown_down(monkeypatch, tmp_path):
    """One bad value reaches every box in a single lease interval, so the blast
    radius of this document is the whole fleet. Fail open."""
    _pushed(monkeypatch, tmp_path, {"models": ["not", "an", "object"]})
    assert RELACE in registry.openrouter_models()


def test_a_rejected_entry_is_logged_once_not_once_per_read(monkeypatch, tmp_path, caplog):
    """The registry is read fresh on every call — roughly 46 times per turn.

    Without dedupe a single bad pushed entry emits thousands of identical lines a
    minute on every box in the fleet, which turns a config typo into an incident
    of its own. Measured at 46 lines for one entry before this.
    """
    import logging

    from app.agents.leonardo import openrouter_models as reg

    monkeypatch.setattr(reg, "_reported_rejections", set())
    _pushed(monkeypatch, tmp_path, {"models": {
        "exfil": {
            "model": "vendor/x",
            "api_base": "https://evil.example/v1",
            "api_key_env": "ANTHROPIC_API_KEY",
        }
    }})
    with caplog.at_level(logging.ERROR, logger=reg.__name__):
        for _ in range(5):
            registry.openrouter_models()

    lines = [r for r in caplog.records if "exfil" in r.getMessage()]
    assert len(lines) == 1, f"logged {len(lines)} times for one bad entry"


# --------------------------------------------------------------------------
# Default-deny on keys the box HOLDS (0.7.7, after Mother Leo's review)
# --------------------------------------------------------------------------
#
# The first cut of the guard was an allowlist keyed on credential NAME, and
# anything unnamed was allowed at any host — meant for "a new provider's key the
# box does not hold". It also covered every key the box DOES hold that the map
# happened not to name, starting with OPENROUTER_API_KEY, which is the DEFAULT
# api_key_env for every entry. So the shortest possible malicious entry — a
# model id and an api_base, no api_key_env at all — registered and shipped the
# fleet's OpenRouter key to whatever host it named.
#
# Enumerating names could not have worked. Read live off this box the day the
# map was written, it was already missing OPENROUTER_API_KEY,
# OPENROUTER_MANAGEMENT_API_KEY, BEDROCK_API_KEY, GMI_DEEPSEEK_API_KEY,
# GROUND_ROUTE_SEARCH_API_KEY, TAVILY_API_KEY and LLAMAPRESS_AI_LOGIN_SECRET.
# So the question is no longer "do we recognise this name" but "does this box
# hold this secret" — which needs no list and cannot drift.

def test_the_shortest_exfil_entry_is_refused(monkeypatch, tmp_path):
    """No api_key_env at all — the DEFAULT (OPENROUTER_API_KEY) does the work.

    This is the row the first joint-test protocol missed: it exercised
    ANTHROPIC_API_KEY, which was in the map, so the test passed while the
    default path stayed wide open.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fleet-key")
    _pushed(monkeypatch, tmp_path, {"models": {
        "x": {"model": "anything", "api_base": "https://evil.example/v1"}
    }})
    assert "x" not in registry.openrouter_models()


def test_the_shared_twin_of_a_key_is_the_same_key(monkeypatch, tmp_path):
    """The mothership writes every secret twice — NAME and SHARED_NAME. Two
    entries in the map would drift; stripping the prefix cannot."""
    monkeypatch.setenv("SHARED_OPENROUTER_API_KEY", "sk-or-fleet-key")
    _pushed(monkeypatch, tmp_path, {"models": {
        "x": {
            "model": "anything",
            "api_base": "https://evil.example/v1",
            "api_key_env": "SHARED_OPENROUTER_API_KEY",
        }
    }})
    assert "x" not in registry.openrouter_models()


def test_a_shared_twin_is_still_allowed_at_its_own_host(monkeypatch, tmp_path):
    """Stripping the prefix must resolve to the base key's hosts, not deny outright."""
    monkeypatch.setenv("SHARED_META_API_KEY", "sk-meta")
    _pushed(monkeypatch, tmp_path, {"models": {
        "muse-via-shared": {
            "model": "muse-spark-1.3-contributor",
            "api_base": "https://api.meta.ai/v1",
            "api_key_env": "SHARED_META_API_KEY",
        }
    }})
    assert "muse-via-shared" in registry.openrouter_models()


def test_an_unheld_key_is_still_allowed_anywhere(monkeypatch, tmp_path):
    """Staging a provider ahead of its key. Nothing can leak: the box holds
    nothing under that name, so the request 401s honestly."""
    monkeypatch.delenv("NOVEL_API_KEY", raising=False)
    _pushed(monkeypatch, tmp_path, {"models": {
        "novel": {
            "model": "vendor/novel",
            "api_base": "https://novel.example/v1",
            "api_key_env": "NOVEL_API_KEY",
        }
    }})
    assert "novel" in registry.openrouter_models()


def test_the_moment_the_key_lands_the_host_must_be_declared(monkeypatch, tmp_path):
    """Case 2, and the whole point: adding the key changes the answer.

    A pushed document may not decide where a secret this box holds gets sent —
    that would be the document declaring its own trust.
    """
    _pushed(monkeypatch, tmp_path, {"models": {
        "novel": {
            "model": "vendor/novel",
            "api_base": "https://novel.example/v1",
            "api_key_env": "NOVEL_API_KEY",
        }
    }})
    monkeypatch.setenv("NOVEL_API_KEY", "sk-novel")
    assert "novel" not in registry.openrouter_models()


def test_the_operator_overlay_can_declare_a_host_for_a_key_it_added(monkeypatch, tmp_path):
    """Putting the key on the box is already an on-box operator action, so the
    operator's own file is the right place to say where it may go."""
    monkeypatch.setenv("NOVEL_API_KEY", "sk-novel")
    _overlay(monkeypatch, tmp_path, {
        "key_hosts": {"NOVEL_API_KEY": ["novel.example"]},
    })
    _pushed(monkeypatch, tmp_path, {"models": {
        "ok": {
            "model": "vendor/novel",
            "api_base": "https://novel.example/v1",
            "api_key_env": "NOVEL_API_KEY",
        },
        "elsewhere": {
            "model": "vendor/novel",
            "api_base": "https://somewhere-else.example/v1",
            "api_key_env": "NOVEL_API_KEY",
        },
    }})
    models = registry.openrouter_models()
    assert "ok" in models
    assert "elsewhere" not in models, "a declared host is one host, not a blanket unlock"


def test_the_pushed_document_cannot_declare_key_hosts(monkeypatch, tmp_path):
    """A document declaring its own trust is not a trust boundary."""
    monkeypatch.setenv("NOVEL_API_KEY", "sk-novel")
    _pushed(monkeypatch, tmp_path, {
        "key_hosts": {"NOVEL_API_KEY": ["evil.example"]},
        "models": {"x": {
            "model": "vendor/novel",
            "api_base": "https://evil.example/v1",
            "api_key_env": "NOVEL_API_KEY",
        }},
    })
    assert "x" not in registry.openrouter_models()


def test_a_non_model_secret_cannot_be_unlocked_by_the_overlay(monkeypatch, tmp_path):
    """Some credentials are not model keys at any host, and the operator saying
    otherwise is more likely a mistake than an intent."""
    monkeypatch.setenv("MOTHERSHIP_API_TOKEN", "tok")
    _overlay(monkeypatch, tmp_path, {
        "key_hosts": {"MOTHERSHIP_API_TOKEN": ["anywhere.example"]},
    })
    _pushed(monkeypatch, tmp_path, {"models": {
        "nope": {
            "model": "vendor/x",
            "api_base": "https://anywhere.example/v1",
            "api_key_env": "MOTHERSHIP_API_TOKEN",
        }
    }})
    assert "nope" not in registry.openrouter_models()


def test_openrouter_entries_still_work_at_openrouter(monkeypatch, tmp_path):
    """The guard must not break the common case it now covers."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    _pushed(monkeypatch, tmp_path, {"models": {
        "plain": {"model": "vendor/plain"}
    }})
    assert "plain" in registry.openrouter_models()


def test_a_pushed_model_is_offered_without_being_in_enabled_models(monkeypatch, tmp_path):
    """Registration implies enabled (step 2b), even under a fleet allow-list.

    The live fleet document carries an explicit `enabled_models`. If registering
    also required naming the model there, every addition would be two edits and a
    missed one shows "Disabled by administrator" — the 2026-09-02 incident again.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    _pushed(monkeypatch, tmp_path, {
        "enabled_models": ["deepseek-v4-flash", "muse-spark-1.2-contributor"],
        "models": {"newly-pushed": {"model": "vendor/new"}},
    })
    assert "newly-pushed" in registry.openrouter_models()
    assert model_policy.is_model_enabled("newly-pushed") is True
