"""Model routing survives a provider withdrawal (0.7.7).

Between 2026-08-31 22:47 and 09-01 02:09 UTC Meta answered ``404 model_not_found``
for ``muse-spark-1.2-contributor`` — 87 failed turns across 14 boxes — and then
started serving it again. It was an upstream blip, not a retirement.

That distinction is the whole reason this file exists. If Muse were dead the fix
would be "point the vision model at GLM", a one-line data change; because it is
not dead, swapping one hardcoded model for another hardcoded model fixes nothing
and the next blip reproduces the outage exactly. **The defect is that a model
choice was not reachable from operator config at all**, and these tests pin the
three things that make it reachable:

  * ``roles`` — ordered fallback CHAINS, not a single name, so a dead first
    choice is a slightly worse answer instead of a dead turn;
  * vision resolved through those chains rather than off a compiled tuple, which
    is what let ``DISABLED_MODELS=muse`` be swept across the fleet during the
    incident while ``vision_model()`` kept returning Muse anyway;
  * a per-instance override, so one customer's constraint is not a fleet decision.

The 0.7.6 properties are load-bearing and re-asserted here rather than assumed:
every key validated independently, a malformed key dropped rather than the
payload, and the box always resolving to something it can actually build.
"""
import pytest

from app.agents.leonardo import model_policy

GLM = "glm-5.3-flash-zai"
MUSE = "muse-spark-1.2-contributor"
DS = "deepseek-v4-flash"
DS_VISION = "deepseek-v4-flash-vision-exp"


@pytest.fixture(autouse=True)
def _clean_box(monkeypatch):
    """A box with no keys, no instance.json and no pushed policy.

    ``OPENROUTER_API_KEY`` is cleared along with the rest — it is a real key on
    this dev box, and leaving it set silently turns every "stock box" assertion
    below into an OpenRouter-box assertion.
    """
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {})
    for var in (
        "META_API_KEY", "MODEL_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
        "ENABLED_MODELS", "DISABLED_MODELS", "DEFAULT_LLM_MODEL",
        "MODEL_SWITCHING_ALLOWED", "VISION_MODEL_ALLOWED",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")


def _install_real_remote_policy(monkeypatch, payload):
    """Feed ``payload`` through the REAL remote_policy validator.

    The autouse fixture stubs ``remote_policy`` to ``{}``; a test that is about
    validation has to put the real one back, or it asserts against its own stub.
    """
    from app.services import model_policy_store

    monkeypatch.setattr(model_policy_store, "load", lambda: payload)
    monkeypatch.setattr(model_policy, "remote_policy", _REAL_REMOTE_POLICY)


_REAL_REMOTE_POLICY = model_policy.remote_policy


# ---------------------------------------------------------------------------
# 1. `roles` is accepted, validated per key, and never poisons the payload
# ---------------------------------------------------------------------------

def test_a_pushed_roles_document_reaches_the_box(monkeypatch):
    _install_real_remote_policy(monkeypatch, {
        "roles": {"chat": [GLM, DS], "vision": [GLM, DS_VISION]},
    })
    assert model_policy.remote_policy()["roles"] == {
        "chat": [GLM, DS],
        "vision": [GLM, DS_VISION],
    }


def test_the_store_does_not_strip_the_new_keys(monkeypatch, tmp_path):
    """``model_policy_store`` has its own allow-list, and a key missing from it
    is dropped before the validator ever sees it — a silent no-op that looks
    exactly like a mothership bug from the far side."""
    from app.services import model_policy_store

    monkeypatch.setenv("MODEL_POLICY_PATH", str(tmp_path / "model_policy.json"))
    model_policy_store.save({
        "roles": {"chat": [GLM]},
        "instance_overrides": {"default_model": DS},
    })
    stored = model_policy_store.load()
    assert stored["roles"] == {"chat": [GLM]}
    assert stored["instance_overrides"] == {"default_model": DS}


def test_a_malformed_roles_value_is_dropped_and_the_rest_still_applies(monkeypatch):
    """The blast-radius rule: one bad value reaches every box in a lease interval."""
    _install_real_remote_policy(monkeypatch, {
        "default_model": GLM,
        "roles": "chat=glm",          # a string, not a mapping
    })
    policy = model_policy.remote_policy()
    assert "roles" not in policy
    assert policy["default_model"] == GLM


def test_one_malformed_role_does_not_drop_the_others(monkeypatch):
    _install_real_remote_policy(monkeypatch, {
        "roles": {"chat": [GLM, DS], "vision": "not-a-list", "empty": []},
    })
    assert model_policy.remote_policy()["roles"] == {"chat": [GLM, DS]}


def test_a_role_chain_of_non_strings_is_dropped(monkeypatch):
    _install_real_remote_policy(monkeypatch, {
        "roles": {"chat": [GLM, 7, None]},
    })
    assert "roles" not in model_policy.remote_policy()


# ---------------------------------------------------------------------------
# 2. A chain resolves to the first entry this box can actually serve
# ---------------------------------------------------------------------------

def test_a_chain_walks_past_a_model_this_box_holds_no_key_for(monkeypatch):
    """The point of a chain: entry 1 is an intent, not a guarantee."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"chat": [GLM, DS]}})
    assert model_policy.resolve_role("chat") == DS


def test_a_chain_walks_past_a_disabled_model(monkeypatch):
    """An operator disable is a routing fact, not just a dropdown fact.

    This is the half that was missing during the incident: DISABLED_MODELS was
    swept fleet-wide and the resolver kept handing back the banned model anyway.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("DISABLED_MODELS", GLM)
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"chat": [GLM, DS]}})
    assert model_policy.resolve_role("chat") == DS


def test_a_chain_walks_past_a_model_reporting_itself_retired(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"chat": [GLM, DS]}})
    monkeypatch.setattr(model_policy.model_health, "is_gone", lambda n: n == GLM)
    assert model_policy.resolve_role("chat") == DS


def test_a_chain_with_nothing_runnable_resolves_to_nothing(monkeypatch):
    """"" hands the decision back to the normal walk instead of naming a model
    that 401s on every turn — the invariant that outranks the operator."""
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"chat": [GLM]}})
    assert model_policy.resolve_role("chat") == ""


def test_an_unconfigured_role_falls_back_to_the_compiled_floor(monkeypatch):
    """No policy pushed = 0.7.6 behaviour, byte for byte."""
    assert model_policy.role_chain("vision") == list(model_policy._VISION_MODELS)
    assert model_policy.role_chain("chat") == []


# ---------------------------------------------------------------------------
# 3. roles.chat wins over default_model; default_model still works alone
# ---------------------------------------------------------------------------

def test_roles_chat_beats_default_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {
        "default_model": MUSE,
        "roles": {"chat": [DS]},
    })
    assert model_policy.configured_default_model() == DS


def test_default_model_alone_still_works(monkeypatch):
    """A 0.7.6-era policy document keeps working unchanged."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {"default_model": DS})
    assert model_policy.configured_default_model() == DS


def test_an_unrunnable_roles_chat_falls_through_to_default_model(monkeypatch):
    """A chain that resolves to nothing must not shadow the older spelling."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {
        "default_model": DS,
        "roles": {"chat": [GLM]},        # no OpenRouter key on this box
    })
    assert model_policy.configured_default_model() == DS


def test_a_bogus_first_choice_still_leaves_the_box_runnable(monkeypatch):
    """Acceptance: a deliberately bogus id as roles.chat[0] must not kill chat."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {
        "roles": {"chat": ["totally-not-a-model", DS]},
    })
    assert model_policy.resolve_role("chat") == DS
    assert model_policy.enabled_default_model() == DS


# ---------------------------------------------------------------------------
# 4. Vision follows policy (absorbs the superseded ticket)
# ---------------------------------------------------------------------------

def test_openrouter_box_resolves_vision_to_glm(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    assert model_policy.vision_model() == GLM


def test_stock_box_resolves_vision_to_the_deepseek_sibling(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    assert model_policy.vision_model() == DS_VISION


def test_the_deepseek_sibling_is_still_enabled_on_a_stock_box(monkeypatch):
    """THE regression the previous attempt hit. Step 2a auto-enables whatever
    this box resolves to; when the resolved model stopped being _VISION_MODELS[0]
    that auto-enable silently stopped covering it, and a stock box lost images."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    assert model_policy.is_model_enabled(DS_VISION) is True


def test_roles_vision_overrides_the_compiled_tuple(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"vision": [DS_VISION, GLM]}})
    assert model_policy.vision_model() == DS_VISION


def test_a_disabled_vision_model_is_routed_around(monkeypatch):
    """During the incident DISABLED_MODELS=muse was swept fleet-wide and
    vision_model() went right on returning Muse."""
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("DISABLED_MODELS", MUSE)
    assert model_policy.vision_model() == DS_VISION


def test_vision_never_names_a_model_the_box_has_no_key_for(monkeypatch):
    """Acceptance, stated as an invariant over every reachable chain."""
    for keys in ([], ["DEEPSEEK_API_KEY"], ["META_API_KEY"], ["OPENROUTER_API_KEY"],
                 ["DEEPSEEK_API_KEY", "META_API_KEY", "OPENROUTER_API_KEY"]):
        for var in ("DEEPSEEK_API_KEY", "META_API_KEY", "OPENROUTER_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        for var in keys:
            monkeypatch.setenv(var, "test-key")
        resolved = model_policy.vision_model()
        assert resolved == "" or model_policy.has_provider_key(resolved), (
            f"keys={keys} resolved to {resolved!r}, which this box cannot build"
        )


# ---------------------------------------------------------------------------
# 5. Per-instance override
# ---------------------------------------------------------------------------

def test_an_instance_override_beats_the_fleet_document(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    _install_real_remote_policy(monkeypatch, {
        "default_model": MUSE,
        "roles": {"chat": [MUSE]},
        "instance_overrides": {"roles": {"chat": [DS]}},
    })
    assert model_policy.remote_policy()["roles"] == {"chat": [DS]}
    assert model_policy.configured_default_model() == DS


def test_an_instance_override_leaves_untouched_fleet_keys_alone(monkeypatch):
    _install_real_remote_policy(monkeypatch, {
        "default_model": MUSE,
        "enabled_models": [MUSE, DS],
        "instance_overrides": {"default_model": DS},
    })
    policy = model_policy.remote_policy()
    assert policy["default_model"] == DS
    assert policy["enabled_models"] == [MUSE, DS]


def test_an_instance_override_cannot_un_disable_a_model(monkeypatch):
    """The trap in §5 of the ticket. Disables UNION across every source, in both
    directions: a remote ban that a narrower scope can lift is not a ban."""
    _install_real_remote_policy(monkeypatch, {
        "disabled_models": [MUSE],
        "instance_overrides": {"disabled_models": [GLM]},
    })
    assert set(model_policy.remote_policy()["disabled_models"]) == {MUSE, GLM}
    assert model_policy.is_model_enabled(MUSE) is False


def test_a_malformed_instance_override_is_dropped_not_the_document(monkeypatch):
    _install_real_remote_policy(monkeypatch, {
        "default_model": DS,
        "instance_overrides": ["not", "an", "object"],
    })
    assert model_policy.remote_policy()["default_model"] == DS


# ---------------------------------------------------------------------------
# 6. The resolved routing is debuggable without SSH
# ---------------------------------------------------------------------------

def test_the_policy_report_names_which_source_won(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    _install_real_remote_policy(monkeypatch, {
        "instance_overrides": {"roles": {"chat": [DS]}},
    })
    report = model_policy.policy_report()
    assert report["default_model"]["value"] == DS
    assert report["default_model"]["source"] == "mothership-instance"
    assert report["roles"]["chat"] == [DS]


def test_the_policy_report_distinguishes_fleet_from_instance(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    _install_real_remote_policy(monkeypatch, {"default_model": DS})
    assert model_policy.policy_report()["default_model"]["source"] == "mothership-fleet"


def test_the_policy_report_names_the_compiled_floor_when_nothing_is_pushed(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    report = model_policy.policy_report()
    assert report["default_model"]["source"] == "compiled"
    assert report["vision_model"]["source"] == "compiled"


def test_available_models_publishes_the_policy_report():
    """The endpoint is how an on-call engineer reads the resolved routing."""
    import inspect

    from app.routers import api

    src = inspect.getsource(api.available_models)
    assert "policy_report()" in src


@pytest.mark.asyncio
async def test_available_models_really_serves_the_policy_block(async_client, monkeypatch):
    """Over HTTP, not by reading the source: a debugging aid that only exists in
    a structural assertion is an aid nobody can actually reach."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    _install_real_remote_policy(monkeypatch, {
        "instance_overrides": {"roles": {"chat": [DS]}},
    })

    body = (await async_client.get("/api/available-models")).json()

    assert body["policy"]["default_model"] == {"value": DS, "source": "mothership-instance"}
    assert body["policy"]["roles"] == {"chat": [DS]}
    # No key name, key value or path leaks into a payload every signed-in user reads.
    rendered = str(body["policy"])
    assert "API_KEY" not in rendered and ".leonardo" not in rendered


# ---------------------------------------------------------------------------
# 7. Rung 2 follows the chain, so a call-time failure lands where told
# ---------------------------------------------------------------------------
#
# Resolution (above) only covers what the box can see BEFORE the request: a
# missing key, a ban, a known retirement. The 2026-08-31 outage was none of
# those — the model was configured, keyed and enabled, and answered 404 at call
# time. `fallback_model` is what runs then, and an operator who wrote an ordered
# chain has already said where to go next.

def test_rung_2_takes_the_next_entry_in_the_chat_chain(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"chat": [GLM, DS]}})
    assert model_policy.fallback_model(GLM) == DS


def test_rung_2_skips_chain_entries_already_tried(monkeypatch):
    """A turn must not bounce between two entries it has already burned."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("ENABLED_MODELS", f"{GLM},{DS},{MUSE}")
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"chat": [GLM, DS]}})
    assert model_policy.fallback_model(GLM, exclude={DS}) != DS


def test_rung_2_falls_back_to_the_policy_walk_when_the_chain_is_exhausted(monkeypatch):
    """A chain is a preference, not a cage: running off the end of it hands the
    question back to the normal policy walk rather than ending the turn.

    The allow-list here is what makes DeepSeek a legal answer at all. That is
    deliberate and is the 0.7.6 rule this does not disturb: a box that names no
    allow-list is NOT offering "every model that happens to have a key", so on
    such a box a one-entry chain really does leave rung 2 with nowhere to go —
    which is the operator's own choice, expressed twice.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("ENABLED_MODELS", f"{GLM},{DS}")
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"chat": [GLM]}})
    assert model_policy.fallback_model(GLM) == DS


def test_an_image_turn_falls_back_along_the_vision_chain(monkeypatch):
    """The image rule survives the rewrite: answering without the screenshot is
    worse than failing, so a vision turn never lands on a text-only model."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", f"{GLM},{DS_VISION}")
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"roles": {"vision": [GLM, DS_VISION]}})
    assert model_policy.fallback_model(GLM, needs_vision=True) == DS_VISION
