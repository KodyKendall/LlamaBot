"""Guard the Anthropic-only `cache_control` kwarg against non-Anthropic providers.

Real incident (mbc-alumni, a paying Pro box, 2026-07-02): every agent turn 500'd with

    TypeError: Completions.create() got an unexpected keyword argument 'cache_control'

because an agent passed `cache_control={"type": "ephemeral"}` as an **invoke kwarg**
unconditionally. That kwarg is Anthropic-only; the OpenAI-compatible client we use for
`deepseek-v4-flash` fleet-wide rejects it.

The immediate crash was fixed with a copy-pasted `startswith("claude")` guard in the
caller. These tests pin the hardened version: one shared predicate, one shared invoke
helper, and no literal provider checks left at the call sites to be dropped in a
refactor or copied into a new agent.
"""

import inspect

import pytest

from app.agents.leonardo.llm_factory import supports_prompt_caching, invoke_with_cache


class RecordingRunnable:
    """Stands in for a bound chat model; records how it was invoked."""

    def __init__(self):
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return "response"


class StrictOpenAIStyleRunnable:
    """Mimics an OpenAI-compatible client: unknown kwargs are a TypeError."""

    def invoke(self, messages):
        return "response"


# --------------------------------------------------------------------------
# supports_prompt_caching
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model",
    ["claude-4.5-sonnet", "claude-opus-5", "anthropic.claude-3-5-sonnet-20241022-v2:0"],
)
def test_anthropic_models_support_prompt_caching(model):
    assert supports_prompt_caching(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4-flash",
        "deepseek-v4-flash-fireworks",
        "gemini-3-flash",
        "gpt-5",
        "fake-llm",
        "",
        None,
    ],
)
def test_non_anthropic_models_do_not_support_prompt_caching(model):
    assert supports_prompt_caching(model) is False


# --------------------------------------------------------------------------
# invoke_with_cache
# --------------------------------------------------------------------------

def test_invoke_with_cache_omits_kwarg_for_deepseek():
    runnable = RecordingRunnable()
    invoke_with_cache(runnable, ["msg"], "deepseek-v4-flash")

    _messages, kwargs = runnable.calls[0]
    assert "cache_control" not in kwargs


def test_invoke_with_cache_passes_kwarg_for_claude():
    runnable = RecordingRunnable()
    invoke_with_cache(runnable, ["msg"], "claude-4.5-sonnet")

    _messages, kwargs = runnable.calls[0]
    assert kwargs["cache_control"] == {"type": "ephemeral"}


def test_invoke_with_cache_does_not_crash_a_strict_openai_client():
    """The exact production crash: a client that rejects unknown kwargs."""
    assert invoke_with_cache(StrictOpenAIStyleRunnable(), ["msg"], "deepseek-v4-flash") == "response"


# --------------------------------------------------------------------------
# No literal provider checks left at the call sites
# --------------------------------------------------------------------------

def test_ai_builder_agent_has_no_inline_cache_control_guard():
    """`rails_ai_builder_agent` was the only agent passing cache_control as a kwarg.

    It must now route through the shared helper, so a future copy-paste or refactor
    cannot silently drop the guard and reintroduce a fleet-wide crash.
    """
    from app.agents.leonardo.rails_ai_builder_agent import nodes

    src = inspect.getsource(nodes)
    assert "cache_control={" not in src, (
        "a raw cache_control= invoke kwarg is back in rails_ai_builder_agent; "
        "use invoke_with_cache() from llm_factory instead"
    )
    assert "invoke_with_cache" in src


def test_no_agent_passes_cache_control_as_an_invoke_kwarg():
    """Fleet-wide sweep: content-block cache_control is fine, invoke kwargs are not."""
    from pathlib import Path

    agents_root = Path(__file__).resolve().parents[1] / "agents"
    # llm_factory owns the one sanctioned kwarg call site (inside invoke_with_cache).
    exempt = {Path("leonardo/llm_factory.py")}

    offenders = []
    for path in agents_root.rglob("*.py"):
        if path.relative_to(agents_root) in exempt:
            continue
        text = path.read_text(encoding="utf-8")
        # `"cache_control": {...}` inside a SystemMessage content block is the safe
        # pattern (non-Anthropic providers ignore it). `cache_control={...}` as a
        # Python kwarg is the one that raises TypeError.
        if "cache_control={" in text:
            offenders.append(str(path.relative_to(agents_root)))

    assert offenders == [], (
        f"these agents pass cache_control as an invoke kwarg: {offenders}. "
        "Route them through llm_factory.invoke_with_cache()."
    )
