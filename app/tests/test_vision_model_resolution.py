"""The box's vision model is resolved, not hardcoded (0.7.5).

Before this, image understanding anywhere in the fleet required a META key:
``VISION_MODEL`` was the constant ``muse-spark-1.2-contributor`` in both
``model_policy`` and ``chat/index.js``, so a DeepSeek-only box refused image
attachments outright with "no image-capable model is configured".

``vision_model()`` now picks whichever vision model the box holds a key for, and
``/api/available-models`` reports it so the frontend auto-switch targets the same
one. These tests pin the three properties that make that safe:

  * it never names a model this box cannot build (the whole point — a wrong name
    here means every image send 401s),
  * a box with no vision key at all still resolves to "" rather than guessing,
  * and the policy actually lets the resolved model through, which is what a
    default allow-list would otherwise block.

0.7.7 turned the compiled tuple into a FLOOR: a pushed ``roles.vision`` replaces
it, and an explicitly disabled model is routed around. Those two live in
``test_model_routing_roles.py``; what stays here is the resolution the compiled
floor produces on each shape of box, which is what a box with no pushed policy
actually runs — still most of the fleet.
"""
import pytest

from app.agents.leonardo import model_policy
from app.agents.leonardo.model_capabilities import get_model_capabilities


def _model_dispatch_source(llm_factory) -> str:
    """The source text where get_llm decides which client to build.

    Both halves, because 0.7.5 split construction into ``_build_client`` so the
    stall guard (``_apply_stream_chunk_timeout``) could be applied in ONE place
    instead of at eleven ``ChatOpenAI(`` call sites. The policy gate stayed in
    ``get_llm``; the per-model branches moved. These structural guards care about
    the dispatch, not about which of the two functions currently holds it.
    """
    import inspect

    return inspect.getsource(llm_factory.get_llm) + inspect.getsource(
        llm_factory._build_client
    )



GLM = "glm-5.3-flash-zai"
MUSE = "muse-spark-1.2-contributor"
DS_VISION = "deepseek-v4-flash-vision-exp"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No keys, no policy config — each test opts into exactly what it needs."""
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {})
    for var in (
        "META_API_KEY", "MODEL_API_KEY", "DEEPSEEK_API_KEY",
        # 0.7.7: GLM joined the vision floor ahead of Muse, so OPENROUTER_API_KEY
        # became a vision key. It is a REAL key on the dev box, and leaving it set
        # here silently turns every "box with only key X" case below into a
        # "box with key X and OpenRouter" case — which is how the first attempt at
        # this change produced eight failures that all looked like assertion rot.
        "OPENROUTER_API_KEY",
        "ENABLED_MODELS", "DISABLED_MODELS",
        "MODEL_SWITCHING_ALLOWED", "VISION_MODEL_ALLOWED",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")


# --- resolution ------------------------------------------------------------

def test_openrouter_keyed_box_resolves_to_glm(monkeypatch):
    """0.7.7: the vision target follows the fleet default text model.

    GLM took the fleet default on 2026-08-31 and is multimodal, so on the common
    box there is now nothing to switch TO — the auto-switch only fires for a user
    who manually moved to a text-only model, which is the property that made Muse
    the head of this list in the first place.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    assert model_policy.vision_model() == GLM


def test_meta_keyed_box_still_resolves_to_muse(monkeypatch):
    """Muse was NOT retired — the 2026-08-31 404s were a 3h22m upstream blip that
    recovered — so a box with a META key and no OpenRouter key keeps its vision
    exactly as before. Demoting Muse was about following the default, not about
    routing around a dead model."""
    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    assert model_policy.vision_model() == MUSE


def test_deepseek_only_box_resolves_to_the_deepseek_vision_model(monkeypatch):
    """The case this feature exists for: vision on the key the box already has."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    assert model_policy.vision_model() == DS_VISION


def test_box_with_no_vision_key_resolves_to_empty(monkeypatch):
    """"" is a real answer, not a bug: it is what makes the frontend say "no
    image-capable model is configured" instead of sending an image to a model it
    cannot authenticate against."""
    assert model_policy.vision_model() == ""


def test_resolved_vision_model_can_always_see_images():
    """A resolver that returned a text-only model would produce exactly the 400
    (`unknown variant image_url`) the capability table exists to prevent."""
    for name in model_policy._VISION_MODELS:
        assert get_model_capabilities(name)["images"] is True


def test_resolved_vision_model_is_buildable(monkeypatch):
    """Every candidate must have a build path — a resolver naming a model that
    falls through to the DeepSeek text default would silently drop images.

    Two legal paths since 0.7.7, because the head of the floor is now a registry
    entry: a hand-written ``model_name == "..."`` branch, or a block in the
    OpenRouter registry, which ``get_llm`` serves from ONE generic branch (that
    is the whole point of the registry — adding an endpoint touches no code).
    Asserting only the first would have failed GLM while GLM built perfectly.
    """
    from app.agents.leonardo import llm_factory
    from app.agents.leonardo.openrouter_models import get_openrouter_model

    src = _model_dispatch_source(llm_factory)
    for name in model_policy._VISION_MODELS:
        assert (
            f'model_name == "{name}"' in src or get_openrouter_model(name) is not None
        ), f"{name} is on the vision floor but nothing can build it"


# --- policy: the resolved model has to actually be selectable ---------------

def test_deepseek_vision_is_enabled_on_a_stock_box_when_vision_is_on(monkeypatch):
    """The compiled default allow-list names only the blessed text models, so
    without step 2a an operator would have to hand-edit ENABLED_MODELS on every
    box before a single image upload worked."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    assert model_policy.is_model_enabled(DS_VISION) is True


def test_glm_vision_is_enabled_on_an_openrouter_box_when_vision_is_on(monkeypatch):
    """Step 2a has to follow the resolver wherever it lands, not stay pinned to
    whatever used to be first in the tuple."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    assert model_policy.is_model_enabled(GLM) is True


def test_deepseek_vision_is_disabled_when_vision_is_off(monkeypatch):
    """VISION_MODEL_ALLOWED stays the operator's opt-in. Turning vision off must
    close the model too, not just hide the upload button."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "false")
    assert model_policy.is_model_enabled(DS_VISION) is False


def test_vision_defaults_off_so_the_new_model_stays_closed(monkeypatch):
    """Vision is still an explicit opt-in. Adding a model must not switch image
    understanding on across the fleet by itself."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    assert model_policy.vision_allowed() is False
    assert model_policy.is_model_enabled(DS_VISION) is False


def test_explicit_disable_beats_the_vision_fail_open(monkeypatch):
    """Step 1 still wins over step 2a — an operator can close it even with vision on."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    monkeypatch.setenv("DISABLED_MODELS", DS_VISION)
    assert model_policy.is_model_enabled(DS_VISION) is False


def test_vision_fail_open_does_not_open_other_models(monkeypatch):
    """Only the ONE resolved vision model gets the exemption."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    assert model_policy.is_model_enabled("gpt-5-codex") is False
    assert model_policy.is_model_enabled("gemini-3-flash") is False
    # ...including the vision model this box has no key for.
    assert model_policy.is_model_enabled(MUSE) is True  # fail-open default, not vision
    assert model_policy.is_model_enabled("deepseek-v4-pro") is False


def test_vision_model_is_never_the_box_default_text_model(monkeypatch):
    """It is a switch target, not a general-purpose model. If it could win the
    enabled_default_model walk, every text turn on the box would run on it."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    # Force the walk: disable both models the default would normally resolve to.
    monkeypatch.setenv("DISABLED_MODELS", f"{MUSE},deepseek-v4-flash")
    assert model_policy.enabled_default_model() != DS_VISION


# --- the endpoint contract the frontend depends on -------------------------

def test_available_models_reports_the_resolved_vision_model():
    """index.js reads `vision_model` off this payload to pick its auto-switch
    target. A missing field silently reverts the frontend to hardcoded Muse."""
    import inspect

    from app.routers import api

    src = inspect.getsource(api.available_models)
    assert '"vision_model": vision_model()' in src
