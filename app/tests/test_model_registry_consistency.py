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

    src = inspect.getsource(llm_factory.get_llm)
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

    src = inspect.getsource(llm_factory.get_llm)
    branch = src.split(f'model_name == "{LUNA}"', 1)[1].split("if model_name ==", 1)[0]
    assert 'model="gpt-5.6-luna"' in branch
    assert 'model="gpt-5.6"' not in branch, "the bare gpt-5.6 alias routes to Sol, not Luna"


def test_luna_is_not_the_default_model():
    """Adding an option must not change what the fleet actually runs."""
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert DEFAULT_LLM_MODEL == "deepseek-v4-flash"
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

    src = inspect.getsource(llm_factory.get_llm)
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

    src = inspect.getsource(llm_factory.get_llm)
    branch = src.split(f'model_name == "{MUSE}"', 1)[1].split("if model_name ==", 1)[0]
    assert '"muse-spark-1.2-contributor"' in branch


def test_muse_is_not_the_default_model():
    """Adding an option must not change what the fleet actually runs."""
    from app.agents.leonardo.llm_factory import DEFAULT_LLM_MODEL

    assert DEFAULT_LLM_MODEL == "deepseek-v4-flash"
    assert MUSE != DEFAULT_LLM_MODEL


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


def test_muse_is_not_fail_open():
    """A data-sharing tier must never be one of the always-on models.

    `_FAIL_OPEN_MODELS` entries stay enabled even when an operator's allow-list
    omits them. Muse contributor trains on every prompt that reaches it, so an
    operator who leaves it off an allow-list has to actually get it turned off.
    """
    from app.agents.leonardo.model_policy import _FAIL_OPEN_MODELS

    assert MUSE not in _FAIL_OPEN_MODELS
