"""GPT-6 Astra on the signed-in user's ChatGPT plan (0.7.11).

Astra is served to ChatGPT plans through Codex, the same backend the Luna/Sol
subscription entries already use, so it is one more `-chatgpt` entry rather
than a new integration. There is no API-key twin: Astra is only offered here
as a ChatGPT-plan model.
"""

from pathlib import Path

import pytest

from app.agents.leonardo import model_policy
from app.agents.leonardo.llm_factory import _CHATGPT_SUBSCRIPTION_MODELS
from app.agents.leonardo.model_capabilities import MODEL_CAPABILITIES, ZDR_COMPLIANT

ASTRA = "gpt-6-astra-chatgpt"
CHAT_HTML = Path(__file__).resolve().parents[1] / "frontend" / "chat.html"


@pytest.fixture(autouse=True)
def _unconfigured_box(monkeypatch):
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {})
    for var in ("ENABLED_MODELS", "DISABLED_MODELS", "MODEL_SWITCHING_ALLOWED"):
        monkeypatch.delenv(var, raising=False)


def test_astra_runs_on_the_codex_backend_with_the_openai_id():
    assert _CHATGPT_SUBSCRIPTION_MODELS[ASTRA] == "gpt-6-astra"


def test_astra_is_known_to_the_policy_and_capabilities():
    assert ASTRA in model_policy._KNOWN_MODELS
    assert MODEL_CAPABILITIES[ASTRA] == {"images": True, "video": False, "pdf": False}
    assert ZDR_COMPLIANT[ASTRA] is False


def test_astra_is_enabled_by_default_like_the_other_plan_models():
    """No operator key reaches it; it lights up only once a user connects."""
    assert model_policy.is_model_enabled(ASTRA) is True


def test_astra_is_never_the_box_default():
    assert model_policy.enabled_default_model() != ASTRA


def test_astra_is_offered_in_the_dropdown():
    assert f'value="{ASTRA}"' in CHAT_HTML.read_text()
