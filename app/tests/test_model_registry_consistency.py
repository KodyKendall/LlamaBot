"""Every model offered in the UI must be registered in every backend registry.

Adding a model means touching five places (dropdown, get_llm, capabilities, policy,
api-key map). Miss one and the failure is silent and confusing rather than loud:

  * missing from `MODEL_CAPABILITIES` -> `_unknown_model_defaults_permissive` kicks in,
    the UI offers image upload, and the provider 400s on `image_url`
  * missing from `model_api_keys`      -> `/api/available-models` omits it, so the
    dropdown entry is there but never marked available/unavailable
  * missing from `get_llm`             -> silently falls through to the DeepSeek default,
    so the user picks GPT and gets DeepSeek with no indication

That last pair is exactly the skew that produced the image_url 400s when a fresh
frontend ran against a stale backend. These tests make a half-wired model a red build.
"""

import re
from pathlib import Path

import pytest

from app.agents.leonardo.model_capabilities import MODEL_CAPABILITIES
from app.agents.leonardo.model_policy import _KNOWN_MODELS


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




CHAT_HTML = Path(__file__).resolve().parents[1] / "frontend" / "chat.html"


def _dropdown_models() -> list[str]:
    """Model values offered by the LLM <select> in chat.html.

    Scoped to `data-llamabot="model-select"` — the agent-mode select right above it
    uses the same option markup, and matching both would assert that "engineer" is a
    registered LLM.
    """
    html = CHAT_HTML.read_text(encoding="utf-8")
    block = html.split('data-llamabot="model-select"', 1)[1].split("</select>", 1)[0]
    return re.findall(r'<option value="([^"]+)"', block)


def _api_key_map() -> dict:
    """The model -> required-env-var map from /api/available-models."""
    import inspect

    from app.routers import api

    src = inspect.getsource(api.available_models)
    # Pull the literal keys out of the model_api_keys dict.
    body = src.split("model_api_keys = {", 1)[1].split("}", 1)[0]
    return {m: True for m in re.findall(r'"([^"]+)":', body)}


def test_dropdown_is_not_empty():
    """Guard the parser itself — a silent regex miss would make every test below vacuous."""
    models = _dropdown_models()
    assert len(models) >= 5, f"only parsed {models} from chat.html; the regex likely drifted"


@pytest.mark.parametrize("model", _dropdown_models())
def test_dropdown_model_has_declared_capabilities(model):
    assert model in MODEL_CAPABILITIES, (
        f"{model} is offered in the UI but missing from MODEL_CAPABILITIES, so it "
        "silently gets the permissive default and can 400 on image uploads"
    )


@pytest.mark.parametrize("model", _dropdown_models())
def test_dropdown_model_has_an_api_key_mapping(model):
    """Every dropdown model must have a declared source of credentials.

    Two legitimate kinds:
      * env-var keyed  -> listed in available_models' model_api_keys
      * user-credential -> listed in llm_factory._CHATGPT_SUBSCRIPTION_MODELS,
        where availability is "has this user connected their ChatGPT account",
        which no env var can answer.
    """
    from app.agents.leonardo.llm_factory import _CHATGPT_SUBSCRIPTION_MODELS

    if model in _CHATGPT_SUBSCRIPTION_MODELS:
        pytest.skip("paid for by the user's ChatGPT plan, not an operator API key")
    assert model in _api_key_map(), (
        f"{model} is offered in the UI but missing from model_api_keys, so "
        "/api/available-models can never report whether it's usable"
    )


def test_subscription_models_are_reported_by_available_models():
    """The user-credential models still need a reachability path in the endpoint —
    they are simply resolved from the connection status rather than an env var."""
    import inspect

    from app.routers import api

    src = inspect.getsource(api.available_models)
    assert "_CHATGPT_SUBSCRIPTION_MODELS" in src, (
        "available_models no longer reports the ChatGPT-subscription models, so "
        "they can never light up in the dropdown"
    )


@pytest.mark.parametrize("model", _dropdown_models())
def test_dropdown_model_is_known_to_the_policy(model):
    assert model in _KNOWN_MODELS, (
        f"{model} is offered in the UI but missing from model_policy._KNOWN_MODELS"
    )


@pytest.mark.parametrize("model", _dropdown_models())
def test_dropdown_model_builds_a_real_client(model, monkeypatch):
    """get_llm must have an explicit branch — not fall through to the default."""
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    # The subscription models are dispatched as a set membership test rather than
    # one `==` branch each, because they share a single credential-backed client.
    dispatched = (
        f'model_name == "{model}"' in src
        or (
            model in llm_factory._CHATGPT_SUBSCRIPTION_MODELS
            and "_CHATGPT_SUBSCRIPTION_MODELS" in src
        )
    )
    assert dispatched, (
        f"get_llm has no branch for {model}; selecting it silently returns the "
        "DeepSeek default instead of the model the user picked"
    )


# --------------------------------------------------------------------------
# GPT-5.6 Luna
# --------------------------------------------------------------------------

LUNA = "gpt-5.6-luna"


def test_luna_is_offered_in_the_dropdown():
    assert LUNA in _dropdown_models()


def test_luna_supports_images_but_not_video_or_pdf():
    """Luna takes text + image input (OpenAI model card), like the other GPT-5 entries."""
    assert MODEL_CAPABILITIES[LUNA] == {"images": True, "video": False, "pdf": False}


def test_luna_uses_the_openai_key():
    assert LUNA in _api_key_map()


def test_luna_is_known_to_the_policy():
    assert LUNA in _KNOWN_MODELS


def test_luna_builds_an_openai_client_with_the_exact_api_id(monkeypatch):
    """The API id is `gpt-5.6-luna`.

    NOT the bare `gpt-5.6` alias — that routes to Sol, a different (pricier) tier.
    """
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{LUNA}"', 1)[1].split("if model_name ==", 1)[0]
    assert 'model="gpt-5.6-luna"' in branch
    assert 'model="gpt-5.6"' not in branch, "the bare gpt-5.6 alias routes to Sol, not Luna"


def test_luna_is_not_the_default_model():
    """Adding an option must not change what the fleet actually runs."""
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert LUNA != DEFAULT_LLM_MODEL


# --------------------------------------------------------------------------
# Muse Spark 1.2 (Meta) — contributor tier
# --------------------------------------------------------------------------

MUSE = "muse-spark-1.2-contributor"


def test_muse_is_offered_in_the_dropdown():
    assert MUSE in _dropdown_models()


def test_muse_supports_images_video_and_pdf():
    """Muse Spark is fully multimodal — text, images, video, audio, PDF in."""
    assert MODEL_CAPABILITIES[MUSE] == {"images": True, "video": True, "pdf": True}


def test_muse_uses_the_meta_key():
    assert MUSE in _api_key_map()


def test_muse_is_known_to_the_policy():
    assert MUSE in _KNOWN_MODELS


def test_muse_builds_an_openai_compatible_client_against_the_meta_endpoint():
    """Meta's Model API is OpenAI-compatible, so this is ChatOpenAI + a base_url.

    The base_url is NOT optional: without it ChatOpenAI silently talks to
    api.openai.com, which has never heard of `muse-spark-1.2-contributor`.
    """
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{MUSE}"', 1)[1].split("if model_name ==", 1)[0]
    assert "ChatOpenAI(" in branch
    assert "https://api.meta.ai/v1" in branch


def test_muse_pins_the_contributor_tier_id():
    """The tier lives entirely in the model id, and the two differ by ~12x in price.

    `muse-spark-1.2-contributor` is $0.10/$0.20 per 1M tokens and licenses Meta to
    train on the prompts and completions we send it; the plain `muse-spark-1.2` id
    is $1.25/$4.25 and does not. Dropping the suffix is therefore a silent 12x
    bill, and adding it back is a silent data-sharing change — pin it.
    """
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{MUSE}"', 1)[1].split("if model_name ==", 1)[0]
    assert '"muse-spark-1.2-contributor"' in branch


def test_muse_is_the_fleet_default():
    """Kody's 0.7.0 call, made 2026-08-09/10.

    This assertion was inverted a day earlier ("adding an option must not change
    what the fleet runs") — the model landed in the dropdown before the decision
    to run it. The tier caveat that motivated the original guard did not go away;
    it is pinned by test_muse_contributor_tier_stays_escapable below.
    """
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert DEFAULT_LLM_MODEL == MUSE


def test_muse_never_sends_the_openai_key_to_meta(monkeypatch):
    """With no Meta key configured, the OpenAI key must NOT go to api.meta.ai.

    `ChatOpenAI` builds its underlying `openai.OpenAI` client lazily, and that
    SDK falls back to `OPENAI_API_KEY` from the environment whenever api_key is
    None. Since we point base_url at Meta, an instance that has OPENAI_API_KEY
    but no META_API_KEY would put our OpenAI secret in an Authorization header
    addressed to a third party. A placeholder keeps the fallback from firing.
    """
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-never-leave")
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(MUSE)
    key = llm.openai_api_key

    assert key is not None, "api_key=None lets the OpenAI SDK fall back to OPENAI_API_KEY"
    assert key.get_secret_value() != "sk-openai-should-never-leave"


def test_muse_is_fail_open_but_still_disableable():
    """Muse is one of the two always-on models as of 0.7.0 — with an escape hatch.

    It IS the fleet default now, so an allow-list that omits it must not strand
    the box on nothing. But the tier trains on every prompt that reaches it, so
    an operator who genuinely needs it off must still be able to get it off:
    an explicit disable (step 1 of the resolution order) beats fail-open. That
    escape is the whole reason this test still exists.
    """
    from app.agents.leonardo import model_policy

    assert MUSE in model_policy._FAIL_OPEN_MODELS

    original = model_policy._read_instance_config
    model_policy._read_instance_config = lambda: {"disabled_models": [MUSE]}
    try:
        assert model_policy.is_model_enabled(MUSE) is False
    finally:
        model_policy._read_instance_config = original


def test_muse_contributor_tier_stays_escapable():
    """The paid, non-training tier must remain reachable without a code change.

    The two ids differ ~12x in price AND completely in data handling, and the
    tier is encoded ONLY in the model id — so a compliance-bound box moves to
    `muse-spark-1.2` via META_MUSE_MODEL. Making the contributor tier the fleet
    default is exactly what makes that override load-bearing.
    """
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{MUSE}"', 1)[1]
    assert 'os.getenv("META_MUSE_MODEL"' in branch, (
        "the model id must stay overridable per box, or a compliance box has no "
        "way off the training tier"
    )


# --------------------------------------------------------------------------
# Qwen3-8B on our own RunPod GPU (self-hosted vLLM)
# --------------------------------------------------------------------------

RUNPOD_QWEN = "qwen3-8b-runpod"

# Never the real pod URL — the endpoint is env-config-only and must not be
# committed (it is unauthenticated today).
FAKE_POD_URL = "http://runpod-qwen.invalid/v1"


@pytest.fixture
def runpod_qwen_env(monkeypatch):
    """A box pointed at a RunPod endpoint, with the model allowed by policy."""
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", RUNPOD_QWEN)
    monkeypatch.setenv("RUNPOD_QWEN_BASE_URL", FAKE_POD_URL)
    monkeypatch.delenv("RUNPOD_QWEN_MODEL", raising=False)
    monkeypatch.delenv("RUNPOD_QWEN_API_KEY", raising=False)


def test_runpod_qwen_is_offered_in_the_dropdown():
    assert RUNPOD_QWEN in _dropdown_models()


def test_runpod_qwen_is_text_only():
    """Qwen3-8B is the dense text model — no image/video/pdf input."""
    assert MODEL_CAPABILITIES[RUNPOD_QWEN] == {
        "images": False,
        "video": False,
        "pdf": False,
    }


def test_runpod_qwen_availability_is_keyed_on_the_base_url():
    """Availability means "this box is pointed at a pod", not "has a secret".

    The endpoint is self-hosted vLLM and may legitimately run with no API key,
    so keying the dropdown on RUNPOD_QWEN_API_KEY would grey out a working
    model on every box.
    """
    import inspect

    from app.routers import api

    src = inspect.getsource(api.available_models)
    body = src.split("model_api_keys = {", 1)[1].split("}", 1)[0]
    assert f'"{RUNPOD_QWEN}": "RUNPOD_QWEN_BASE_URL"' in body


def test_runpod_qwen_is_known_to_the_policy():
    assert RUNPOD_QWEN in _KNOWN_MODELS


def test_runpod_qwen_base_url_comes_from_the_env(runpod_qwen_env):
    """vLLM is OpenAI-compatible, so this is ChatOpenAI + a base_url — and the
    base_url is load-bearing: without it ChatOpenAI talks to api.openai.com,
    which has never heard of `Qwen/Qwen3-8B`."""
    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(RUNPOD_QWEN)

    assert str(llm.openai_api_base).rstrip("/") == FAKE_POD_URL.rstrip("/")


def test_runpod_qwen_pod_url_is_never_hardcoded():
    """The pod URL is sensitive and rotatable — it lives in .env, never in git."""
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{RUNPOD_QWEN}"', 1)[1].split("if model_name ==", 1)[0]
    assert 'os.getenv("RUNPOD_QWEN_BASE_URL")' in branch
    assert "proxy.runpod.net" not in branch, "the pod URL must not be committed"


def test_runpod_qwen_model_id_defaults_but_stays_overridable(runpod_qwen_env, monkeypatch):
    from app.agents.leonardo.llm_factory import get_llm

    assert get_llm(RUNPOD_QWEN).model_name == "Qwen/Qwen3-8B"

    monkeypatch.setenv("RUNPOD_QWEN_MODEL", "Qwen/Qwen3-14B")
    assert get_llm(RUNPOD_QWEN).model_name == "Qwen/Qwen3-14B"


def test_runpod_qwen_disables_thinking(runpod_qwen_env):
    """Qwen3 emits <think>...</think> inside `content` unless thinking is off,
    which pollutes tool-calling turns. Disabled client-side via the vLLM
    chat_template_kwargs passthrough."""
    from app.agents.leonardo.llm_factory import get_llm

    extra_body = get_llm(RUNPOD_QWEN).extra_body or {}
    assert extra_body["chat_template_kwargs"]["enable_thinking"] is False


def test_runpod_qwen_never_sends_the_openai_key_to_the_pod(runpod_qwen_env, monkeypatch):
    """The pod is unauthenticated today, so api_key=None is tempting — but that
    is exactly what makes the openai SDK fall back to OPENAI_API_KEY and put our
    OpenAI secret in an Authorization header addressed to a RunPod proxy URL."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-never-leave")

    from app.agents.leonardo.llm_factory import get_llm

    key = get_llm(RUNPOD_QWEN).openai_api_key

    assert key is not None, "api_key=None lets the OpenAI SDK fall back to OPENAI_API_KEY"
    assert key.get_secret_value() != "sk-openai-should-never-leave"


def test_runpod_qwen_uses_the_configured_key_when_the_pod_gets_one(runpod_qwen_env, monkeypatch):
    """vLLM can be started with --api-key later; the plumbing must already work."""
    monkeypatch.setenv("RUNPOD_QWEN_API_KEY", "real-runpod-key")

    from app.agents.leonardo.llm_factory import get_llm

    assert get_llm(RUNPOD_QWEN).openai_api_key.get_secret_value() == "real-runpod-key"


def test_runpod_qwen_is_not_the_fleet_default():
    """Adding an option must not change what the fleet actually runs."""
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert RUNPOD_QWEN != DEFAULT_LLM_MODEL


# --------------------------------------------------------------------------
# Muse Glimmer 30B on our own RunPod GPU (self-hosted vLLM)
# --------------------------------------------------------------------------

GLIMMER = "muse-glimmer-30b-runpod"

FAKE_GLIMMER_URL = "http://runpod-glimmer.invalid/v1"


@pytest.fixture
def glimmer_env(monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", GLIMMER)
    monkeypatch.setenv("RUNPOD_GLIMMER_BASE_URL", FAKE_GLIMMER_URL)
    monkeypatch.delenv("RUNPOD_GLIMMER_MODEL", raising=False)
    monkeypatch.delenv("RUNPOD_GLIMMER_API_KEY", raising=False)


def test_glimmer_is_offered_in_the_dropdown():
    assert GLIMMER in _dropdown_models()


def test_glimmer_is_text_only():
    """The base model is multimodal, but this community AWQ checkpoint's vision
    path is unverified — don't advertise what we haven't tested."""
    assert MODEL_CAPABILITIES[GLIMMER] == {
        "images": False,
        "video": False,
        "pdf": False,
    }


def test_glimmer_availability_is_keyed_on_the_base_url():
    import inspect

    from app.routers import api

    src = inspect.getsource(api.available_models)
    body = src.split("model_api_keys = {", 1)[1].split("}", 1)[0]
    assert f'"{GLIMMER}": "RUNPOD_GLIMMER_BASE_URL"' in body


def test_glimmer_is_known_to_the_policy():
    assert GLIMMER in _KNOWN_MODELS


def test_glimmer_base_url_comes_from_the_env(glimmer_env):
    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(GLIMMER)

    assert str(llm.openai_api_base).rstrip("/") == FAKE_GLIMMER_URL.rstrip("/")


def test_glimmer_pod_url_is_never_hardcoded():
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{GLIMMER}"', 1)[1].split("if model_name ==", 1)[0]
    assert 'os.getenv("RUNPOD_GLIMMER_BASE_URL")' in branch
    assert "proxy.runpod.net" not in branch, "the pod URL must not be committed"


def test_glimmer_model_id_defaults_but_stays_overridable(glimmer_env, monkeypatch):
    from app.agents.leonardo.llm_factory import get_llm

    assert get_llm(GLIMMER).model_name == "cyankiwi/Muse-Glimmer-30B-AWQ-INT4"

    monkeypatch.setenv("RUNPOD_GLIMMER_MODEL", "cyankiwi/Muse-Glimmer-30B")
    assert get_llm(GLIMMER).model_name == "cyankiwi/Muse-Glimmer-30B"


def test_glimmer_does_not_send_qwens_thinking_kwarg(glimmer_env):
    """`enable_thinking` is a QWEN chat-template feature, not a portable flag.

    Glimmer's reasoning is separated server-side by vLLM's
    `--reasoning-parser muse_glimmer`, so `content` already arrives clean.
    Forwarding Qwen's kwarg to a template that has never heard of it risks a
    400 for no benefit — copying the sibling branch wholesale is the mistake
    this pins.
    """
    from app.agents.leonardo.llm_factory import get_llm

    extra_body = get_llm(GLIMMER).extra_body or {}
    assert "chat_template_kwargs" not in extra_body
    assert "enable_thinking" not in str(extra_body)


def test_glimmer_never_sends_the_openai_key_to_the_pod(glimmer_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-never-leave")

    from app.agents.leonardo.llm_factory import get_llm

    key = get_llm(GLIMMER).openai_api_key

    assert key is not None, "api_key=None lets the OpenAI SDK fall back to OPENAI_API_KEY"
    assert key.get_secret_value() != "sk-openai-should-never-leave"


def test_glimmer_uses_the_configured_key_when_the_pod_gets_one(glimmer_env, monkeypatch):
    monkeypatch.setenv("RUNPOD_GLIMMER_API_KEY", "real-glimmer-key")

    from app.agents.leonardo.llm_factory import get_llm

    assert get_llm(GLIMMER).openai_api_key.get_secret_value() == "real-glimmer-key"


def test_glimmer_does_not_reuse_the_qwen_env_vars(glimmer_env, monkeypatch):
    """Two self-hosted entries, two independent env triples.

    The pod serves one model at a time today, but a second pod is one API call
    away — and re-pointing RUNPOD_QWEN_MODEL at a Meta checkpoint is exactly the
    hidden provider swap the separate-entry house style exists to prevent.
    """
    monkeypatch.setenv("RUNPOD_QWEN_BASE_URL", "http://wrong-pod.invalid/v1")
    monkeypatch.setenv("RUNPOD_QWEN_MODEL", "Qwen/Qwen3-8B")

    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(GLIMMER)

    assert str(llm.openai_api_base).rstrip("/") == FAKE_GLIMMER_URL.rstrip("/")
    assert llm.model_name == "cyankiwi/Muse-Glimmer-30B-AWQ-INT4"


def test_glimmer_is_not_the_fleet_default():
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert GLIMMER != DEFAULT_LLM_MODEL


def test_the_two_selfhosted_entries_stay_distinct():
    """Glimmer is a sibling entry, not a rename of the qwen one."""
    assert RUNPOD_QWEN in _dropdown_models()
    assert GLIMMER in _dropdown_models()
    assert RUNPOD_QWEN != GLIMMER


# --------------------------------------------------------------------------
# NVIDIA Nemotron 3.5 Lightning 30B-A3B on its own RunPod GPU (self-hosted vLLM)
# --------------------------------------------------------------------------

NEMOTRON = "nemotron-lightning-30b-runpod"

FAKE_NEMOTRON_URL = "http://runpod-nemotron.invalid/v1"


@pytest.fixture
def nemotron_env(monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", NEMOTRON)
    monkeypatch.setenv("RUNPOD_NEMOTRON_BASE_URL", FAKE_NEMOTRON_URL)
    monkeypatch.delenv("RUNPOD_NEMOTRON_MODEL", raising=False)
    monkeypatch.delenv("RUNPOD_NEMOTRON_API_KEY", raising=False)


def test_nemotron_is_offered_in_the_dropdown():
    assert NEMOTRON in _dropdown_models()


def test_nemotron_is_text_only():
    """NemotronH causal LM — no vision path at all."""
    assert MODEL_CAPABILITIES[NEMOTRON] == {
        "images": False,
        "video": False,
        "pdf": False,
    }


def test_nemotron_availability_is_keyed_on_the_base_url():
    import inspect

    from app.routers import api

    src = inspect.getsource(api.available_models)
    body = src.split("model_api_keys = {", 1)[1].split("}", 1)[0]
    assert f'"{NEMOTRON}": "RUNPOD_NEMOTRON_BASE_URL"' in body


def test_nemotron_is_known_to_the_policy():
    assert NEMOTRON in _KNOWN_MODELS


def test_nemotron_base_url_comes_from_the_env(nemotron_env):
    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(NEMOTRON)

    assert str(llm.openai_api_base).rstrip("/") == FAKE_NEMOTRON_URL.rstrip("/")


def test_nemotron_pod_url_is_never_hardcoded():
    """The pod has NO auth — the unguessable URL is the only thing protecting it."""
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{NEMOTRON}"', 1)[1].split("if model_name ==", 1)[0]
    assert 'os.getenv("RUNPOD_NEMOTRON_BASE_URL")' in branch
    assert "proxy.runpod.net" not in branch, "the pod URL must not be committed"


def test_nemotron_model_id_defaults_but_stays_overridable(nemotron_env, monkeypatch):
    from app.agents.leonardo.llm_factory import get_llm

    assert get_llm(NEMOTRON).model_name == (
        "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"
    )

    monkeypatch.setenv("RUNPOD_NEMOTRON_MODEL", "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B")
    assert get_llm(NEMOTRON).model_name == (
        "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B"
    )


def test_nemotron_does_not_send_a_thinking_kwarg(nemotron_env):
    """Reasoning is separated server-side by vLLM's `--reasoning-parser nemotron_v3`,
    so `content` already arrives clean. Qwen's `enable_thinking` is a Qwen
    chat-template feature and has no business on this template."""
    from app.agents.leonardo.llm_factory import get_llm

    extra_body = get_llm(NEMOTRON).extra_body or {}
    assert "chat_template_kwargs" not in extra_body
    assert "enable_thinking" not in str(extra_body)


def test_nemotron_does_not_cap_completion_tokens(nemotron_env):
    """The model burns ~250 reasoning tokens even on trivial prompts, and that
    reasoning is billed against max_tokens while being stripped server-side. A
    low completion cap therefore returns EMPTY content with no error at all —
    verified live on the pod at max_tokens=500. Leave the cap unset."""
    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(NEMOTRON)

    assert getattr(llm, "max_tokens", None) is None
    assert not (llm.extra_body or {}).get("max_tokens")


def test_nemotron_never_sends_the_openai_key_to_the_pod(nemotron_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-never-leave")

    from app.agents.leonardo.llm_factory import get_llm

    key = get_llm(NEMOTRON).openai_api_key

    assert key is not None, "api_key=None lets the OpenAI SDK fall back to OPENAI_API_KEY"
    assert key.get_secret_value() != "sk-openai-should-never-leave"


def test_nemotron_uses_the_configured_key_when_the_pod_gets_one(nemotron_env, monkeypatch):
    monkeypatch.setenv("RUNPOD_NEMOTRON_API_KEY", "real-nemotron-key")

    from app.agents.leonardo.llm_factory import get_llm

    assert get_llm(NEMOTRON).openai_api_key.get_secret_value() == "real-nemotron-key"


def test_nemotron_does_not_reuse_the_sibling_env_vars(nemotron_env, monkeypatch):
    """Three self-hosted entries, three independent env triples — and these are
    genuinely separate pods running concurrently, not one box swapping models."""
    monkeypatch.setenv("RUNPOD_QWEN_BASE_URL", "http://wrong-pod.invalid/v1")
    monkeypatch.setenv("RUNPOD_GLIMMER_BASE_URL", "http://also-wrong.invalid/v1")
    monkeypatch.setenv("RUNPOD_GLIMMER_MODEL", "cyankiwi/Muse-Glimmer-30B-AWQ-INT4")

    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(NEMOTRON)

    assert str(llm.openai_api_base).rstrip("/") == FAKE_NEMOTRON_URL.rstrip("/")
    assert llm.model_name == "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"


def test_nemotron_is_not_the_fleet_default():
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert NEMOTRON != DEFAULT_LLM_MODEL


def test_the_three_selfhosted_entries_stay_distinct():
    """Each pod is its own dropdown entry — never a hidden re-point of another."""
    dropdown = _dropdown_models()
    assert {RUNPOD_QWEN, GLIMMER, NEMOTRON} <= set(dropdown)
    assert len({RUNPOD_QWEN, GLIMMER, NEMOTRON}) == 3


# --------------------------------------------------------------------------
# NVIDIA Nemotron 3.5 Lightning 30B-A3B on Fireworks (serverless)
# --------------------------------------------------------------------------

NEMOTRON_FW = "nemotron-lightning-30b-fireworks"

FIREWORKS_NEMOTRON_ID = "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b"


@pytest.fixture
def nemotron_fw_env(monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", NEMOTRON_FW)
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-test-key")
    monkeypatch.delenv("FIREWORKS_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("FIREWORKS_NEMOTRON_MODEL", raising=False)
    monkeypatch.delenv("FIREWORKS_BASE_URL", raising=False)


def test_nemotron_fw_is_offered_in_the_dropdown():
    assert NEMOTRON_FW in _dropdown_models()


def test_nemotron_fw_is_text_only():
    """Same weights as the RunPod entry — NemotronH causal LM, no vision path."""
    assert MODEL_CAPABILITIES[NEMOTRON_FW] == {
        "images": False,
        "video": False,
        "pdf": False,
    }


def test_nemotron_fw_is_known_to_the_policy():
    assert NEMOTRON_FW in _KNOWN_MODELS


def test_nemotron_fw_availability_accepts_either_fireworks_key():
    """The endpoint must agree with get_llm's precedence, or a box with only one
    of the two key names shows the model greyed out while it would work fine."""
    import inspect

    from app.routers import api

    src = inspect.getsource(api.available_models)
    body = src.split("model_api_keys = {", 1)[1].split("}", 1)[0]
    assert (
        f'"{NEMOTRON_FW}": ("FIREWORKS_API_KEY", "FIREWORKS_DEEPSEEK_API_KEY")' in body
    )


def test_nemotron_fw_routes_to_fireworks(nemotron_fw_env):
    """The whole point of the separate name: Fireworks' endpoint, Fireworks' id."""
    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(NEMOTRON_FW)

    assert str(llm.client._client.base_url).rstrip("/") == (
        "https://api.fireworks.ai/inference/v1"
    )
    assert llm.model_name == FIREWORKS_NEMOTRON_ID


def test_nemotron_fw_model_id_stays_overridable(nemotron_fw_env, monkeypatch):
    from app.agents.leonardo.llm_factory import get_llm

    monkeypatch.setenv("FIREWORKS_NEMOTRON_MODEL", "accounts/fireworks/models/other")
    assert get_llm(NEMOTRON_FW).model_name == "accounts/fireworks/models/other"


def test_nemotron_fw_falls_back_to_the_deployed_key_name(monkeypatch):
    """Boxes today carry FIREWORKS_DEEPSEEK_API_KEY, not FIREWORKS_API_KEY — the
    model must light up on those without an env change."""
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", NEMOTRON_FW)
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.setenv("FIREWORKS_DEEPSEEK_API_KEY", "fw-deepseek-key")

    from app.agents.leonardo.llm_factory import get_llm

    assert get_llm(NEMOTRON_FW).client._client.api_key == "fw-deepseek-key"


def test_nemotron_fw_preserves_reasoning_content(nemotron_fw_env):
    """Fireworks returns Nemotron's thinking in a separate `reasoning_content`
    field and streams it as deltas (verified live 2026-08-16), so this entry needs
    the reasoning-preserving client — a bare ChatOpenAI drops the thinking."""
    from app.agents.leonardo.llm_factory import ChatDeepSeekWithReasoning, get_llm

    assert isinstance(get_llm(NEMOTRON_FW), ChatDeepSeekWithReasoning)


def test_nemotron_fw_does_not_cap_completion_tokens(nemotron_fw_env):
    """Reasoning bills against max_tokens while being stripped from the response,
    so a cap returns EMPTY content with no error — the same trap as the RunPod
    entry. Fireworks imposes no small default of its own."""
    from app.agents.leonardo.llm_factory import get_llm

    llm = get_llm(NEMOTRON_FW)

    assert getattr(llm, "max_tokens", None) in (None, -1)


def test_nemotron_fw_is_a_sibling_of_the_selfhosted_entry():
    """Serverless and self-hosted are separate choices, not a re-point of one."""
    dropdown = _dropdown_models()
    assert {NEMOTRON, NEMOTRON_FW} <= set(dropdown)
    assert NEMOTRON != NEMOTRON_FW


def test_nemotron_fw_is_not_the_fleet_default():
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert NEMOTRON_FW != DEFAULT_LLM_MODEL


# --------------------------------------------------------------------------
# DeepSeek V4 Flash Vision (experimental) — 0.7.5
# --------------------------------------------------------------------------

DS_VISION = "deepseek-v4-flash-vision-exp"


def test_ds_vision_is_offered_in_the_dropdown():
    assert DS_VISION in _dropdown_models()


def test_ds_vision_supports_images_only():
    """Images yes; no video, and PDFs only via DeepSeek's separate Files API,
    which we don't use — so declaring pdf:True would send a `file` block the
    chat-completions endpoint rejects."""
    assert MODEL_CAPABILITIES[DS_VISION] == {
        "images": True, "video": False, "pdf": False,
    }


def test_ds_vision_is_the_only_deepseek_entry_with_vision():
    """The text entries must stay text-only. DeepSeek 400s an image sent to them
    ("This model does not support image"), and MODEL_CAPABILITIES is what stops
    the frontend offering the upload and the stripper leaving it in history."""
    for text_model in (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-gmi",
        "deepseek-v4-flash-fireworks",
    ):
        assert MODEL_CAPABILITIES[text_model]["images"] is False


def test_ds_vision_uses_the_deepseek_key():
    assert DS_VISION in _api_key_map()
    assert _api_key_map()[DS_VISION] is True


def test_ds_vision_is_known_to_the_policy():
    assert DS_VISION in _KNOWN_MODELS


def test_ds_vision_builds_a_reasoning_client_with_the_exact_api_id():
    """The client must be ChatDeepSeekWithReasoning, not a bare ChatDeepSeek:
    the model streams reasoning_content deltas like its text siblings (verified
    live), and a plain client drops the thinking on the floor."""
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{DS_VISION}"', 1)[1].split("if model_name ==", 1)[0]
    assert f'model="{DS_VISION}"' in branch
    assert "ChatDeepSeekWithReasoning(" in branch


def test_ds_vision_goes_to_deepseek_direct_not_a_reseller():
    """No api_base override — it is only on DeepSeek's own API, unlike the text
    model, which has GMI and Fireworks siblings."""
    import inspect

    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{DS_VISION}"', 1)[1].split("if model_name ==", 1)[0]
    assert "api_base" not in branch


def test_ds_vision_is_covered_by_the_deepseek_reasoning_middleware():
    """DeepSeek direct is the strict one about assistant messages carrying
    reasoning_content. The middleware used to gate on the literal string
    "deepseek-v4-flash", which silently skipped every other direct model —
    including v4-pro, which shipped that way for several releases."""
    from app.agents.leonardo.llm_factory import DEEPSEEK_DIRECT_MODELS

    assert DS_VISION in DEEPSEEK_DIRECT_MODELS
    assert "deepseek-v4-pro" in DEEPSEEK_DIRECT_MODELS
    # The resellers must NOT be in it: they reject nothing, and injecting an
    # empty reasoning_content there would be a pointless message rewrite.
    assert "deepseek-v4-flash-gmi" not in DEEPSEEK_DIRECT_MODELS
    assert "deepseek-v4-flash-fireworks" not in DEEPSEEK_DIRECT_MODELS


def test_ds_vision_is_not_the_fleet_default():
    """Adding an option must not change what the fleet actually runs."""
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL, FALLBACK_TEXT_MODEL

    assert DS_VISION != DEFAULT_LLM_MODEL
    assert DS_VISION != FALLBACK_TEXT_MODEL


# --------------------------------------------------------------------------
# Qwen3.8 27B (Hetzner Inference API)
# --------------------------------------------------------------------------

HETZNER_QWEN = "qwen3.8-27b-hetzner"


def test_hetzner_qwen_is_offered_in_the_dropdown():
    assert HETZNER_QWEN in _dropdown_models()


def test_hetzner_qwen_supports_images_but_not_video_or_pdf():
    """Hetzner's published model table lists this one as Text + Image."""
    assert MODEL_CAPABILITIES[HETZNER_QWEN] == {
        "images": True,
        "video": False,
        "pdf": False,
    }


def test_hetzner_qwen_uses_the_hetzner_key():
    assert HETZNER_QWEN in _api_key_map()


def test_hetzner_qwen_is_known_to_the_policy():
    assert HETZNER_QWEN in _KNOWN_MODELS


def test_hetzner_qwen_builds_an_openai_compatible_client_against_hetzner():
    """OpenAI-compatible gateway, so ChatOpenAI + a base_url.

    The base_url is NOT optional: without it ChatOpenAI silently talks to
    api.openai.com, which has never heard of `Qwen3.8-27B`.
    """
    from app.agents.leonardo import llm_factory

    src = _model_dispatch_source(llm_factory)
    branch = src.split(f'model_name == "{HETZNER_QWEN}"', 1)[1].split(
        "if model_name ==", 1
    )[0]
    assert "ChatOpenAI(" in branch
    assert "https://inference.hetzner.com/api/v1" in branch
    assert '"Qwen3.8-27B"' in branch


def test_hetzner_qwen_is_not_the_default_model():
    """Adding an option must not change what the fleet actually runs.

    Especially this one: the Hetzner API is free/experimental and rate limited
    to 10 requests per minute per key, which one agentic turn can exhaust.
    """
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert HETZNER_QWEN != DEFAULT_LLM_MODEL
