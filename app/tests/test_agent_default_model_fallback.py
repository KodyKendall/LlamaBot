"""No agent may hardcode a model id as its 'no llm_model in state' fallback.

The 0.7.0 frontend blocker had a backend twin: every rails_* agent resolved a
missing ``state['llm_model']`` with a literal ``or 'deepseek-v4-flash'``. So a
turn that arrives without an explicit model — which is exactly what the fixed
frontend sends before /api/available-models resolves — ran on DeepSeek no matter
what the fleet default said, and the "vision routes through Muse" rule silently
did not apply either.

The fallback must be the policy-resolved default (``enabled_default_model()``),
which is Muse on a box with a META key and DeepSeek on one without.
"""

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]

# `X or 'some-model'` — the idiom that hardcodes a fallback model.
HARDCODED_FALLBACK = re.compile(
    r"""or\s+['"](?:deepseek-v4-\w+|gemini-[\w.]+|gpt-[\w.]+|claude-[\w.]+|"""
    r"""muse-[\w.]+|qwen[\w.]+)['"]"""
)

SEARCH_ROOTS = (APP_ROOT / "agents", APP_ROOT / "websocket")


def _source_files():
    for root in SEARCH_ROOTS:
        for path in sorted(root.rglob("*.py")):
            yield path


def test_no_agent_hardcodes_a_fallback_model():
    offenders = []
    for path in _source_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if HARDCODED_FALLBACK.search(line):
                offenders.append(f"{path.relative_to(APP_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "These fall back to a hardcoded model when llm_model is absent; use "
        "enabled_default_model() so the fleet default applies:\n  "
        + "\n  ".join(offenders)
    )


def test_missing_llm_model_resolves_to_the_policy_default(monkeypatch):
    """The behavior the sweep protects: absent model -> box's resolved default."""
    from app.agents.leonardo import model_policy

    monkeypatch.setenv("META_API_KEY", "test-key")
    monkeypatch.delenv("MODEL_SWITCHING_ALLOWED", raising=False)
    monkeypatch.delenv("ENABLED_MODELS", raising=False)
    monkeypatch.delenv("DISABLED_MODELS", raising=False)

    state = {}
    assert (state.get("llm_model") or model_policy.enabled_default_model()) == (
        "muse-spark-1.2-contributor"
    )


def test_missing_llm_model_on_a_keyless_box_resolves_to_deepseek(monkeypatch):
    from app.agents.leonardo import model_policy

    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_SWITCHING_ALLOWED", raising=False)
    monkeypatch.delenv("ENABLED_MODELS", raising=False)
    monkeypatch.delenv("DISABLED_MODELS", raising=False)

    state = {}
    assert (state.get("llm_model") or model_policy.enabled_default_model()) == (
        "deepseek-v4-flash"
    )
