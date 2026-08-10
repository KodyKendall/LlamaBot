"""Guard block-list system prompts against providers that only accept a string.

Real incident (Leo Database Mode, 2026-08): every turn 400'd with a Fireworks
complaint about the system message, because the agents build their system prompt
in Anthropic's prompt-caching shape — a LIST of content blocks carrying
`cache_control` — while the box was actually running `deepseek-v4-flash`
remapped by policy to `deepseek-v4-flash-fireworks`. Fireworks (and GMI) expose
an OpenAI-compatible API that strictly requires system `content` to be a plain
string; DeepSeek direct and Anthropic both tolerate the list, which is why this
only surfaced once the fleet moved to Fireworks.

This is the *content-block* sibling of the `cache_control=` invoke-kwarg bug
pinned in test_cache_control_non_anthropic.py. Same rule: one shared predicate,
one shared helper, no literal provider checks at the call sites.
"""

from pathlib import Path

import pytest
from langchain_core.messages import SystemMessage

from app.agents.leonardo.llm_factory import system_message_for_model

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents" / "leonardo"

CACHED_BLOCKS = [
    {"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}
]

# The two OpenAI-compatible gateways serving deepseek-v4-flash on the fleet,
# plus the rest of the non-Anthropic registry.
STRICT_MODELS = [
    "deepseek-v4-flash-fireworks",
    "deepseek-v4-flash-gmi",
    "deepseek-v4-flash",
    "gpt-5.6-luna",
    "gemini-3-flash",
    "qwen3.7-plus",
    "",
    None,
]


# --------------------------------------------------------------------------
# system_message_for_model
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model", STRICT_MODELS)
def test_blocks_are_flattened_for_non_anthropic(model):
    out = system_message_for_model(SystemMessage(content=CACHED_BLOCKS), model)
    assert isinstance(out, SystemMessage)
    assert out.content == "SYSTEM PROMPT"


@pytest.mark.parametrize("model", ["claude-4.5-haiku", "claude-4.5-sonnet"])
def test_blocks_are_preserved_for_anthropic(model):
    """Flattening Claude would silently kill prompt caching (~90% of input cost)."""
    msg = SystemMessage(content=CACHED_BLOCKS)
    out = system_message_for_model(msg, model)
    assert out is msg
    assert out.content[0]["cache_control"] == {"type": "ephemeral"}


def test_dict_system_message_is_flattened_in_place():
    """The raw StateGraph agents build a {"role": "system"} dict, not a SystemMessage."""
    out = system_message_for_model(
        {"role": "system", "content": CACHED_BLOCKS}, "deepseek-v4-flash-fireworks"
    )
    assert out == {"role": "system", "content": "SYSTEM PROMPT"}


def test_multiple_blocks_are_joined():
    out = system_message_for_model(
        SystemMessage(content=[
            {"type": "text", "text": "A"},
            {"type": "text", "text": "B", "cache_control": {"type": "ephemeral"}},
        ]),
        "deepseek-v4-flash-fireworks",
    )
    assert out.content == "A\n\nB"


def test_plain_string_content_is_untouched():
    msg = SystemMessage(content="already a string")
    assert system_message_for_model(msg, "deepseek-v4-flash-fireworks") is msg
    assert system_message_for_model("bare string", "deepseek-v4-flash-fireworks") == "bare string"


def test_none_is_untouched():
    assert system_message_for_model(None, "deepseek-v4-flash-fireworks") is None


# --------------------------------------------------------------------------
# DynamicModelMiddleware — covers every create_agent-based mode
# --------------------------------------------------------------------------

class _FakeRequest:
    """Stands in for langchain's ModelRequest (only what the middleware touches)."""

    def __init__(self, llm_model, system_message):
        self.state = {"llm_model": llm_model}
        self.system_message = system_message
        self.model = None

    def override(self, **overrides):
        clone = _FakeRequest(self.state["llm_model"], self.system_message)
        for k, v in overrides.items():
            setattr(clone, k, v)
        return clone


def _middleware(monkeypatch):
    import app.agents.leonardo.rails_agent.middleware as mw
    monkeypatch.setattr(mw, "get_llm", lambda name: f"<model:{name}>")
    return mw.DynamicModelMiddleware()


def test_middleware_flattens_system_message_for_fireworks(monkeypatch):
    """Database Mode's exact failure: a block-list prompt reaching Fireworks."""
    seen = {}
    request = _FakeRequest("deepseek-v4-flash-fireworks", SystemMessage(content=CACHED_BLOCKS))

    _middleware(monkeypatch).wrap_model_call(
        request, lambda req: seen.setdefault("req", req)
    )
    assert seen["req"].system_message.content == "SYSTEM PROMPT"
    assert seen["req"].model == "<model:deepseek-v4-flash-fireworks>"


def test_middleware_preserves_system_blocks_for_anthropic(monkeypatch):
    seen = {}
    request = _FakeRequest("claude-4.5-haiku", SystemMessage(content=CACHED_BLOCKS))

    _middleware(monkeypatch).wrap_model_call(
        request, lambda req: seen.setdefault("req", req)
    )
    assert seen["req"].system_message.content == CACHED_BLOCKS


@pytest.mark.asyncio
async def test_async_middleware_flattens_system_message_for_gmi(monkeypatch):
    """The websocket chat path is the async one — it must flatten too."""
    seen = {}
    request = _FakeRequest("deepseek-v4-flash-gmi", SystemMessage(content=CACHED_BLOCKS))

    async def handler(req):
        seen["req"] = req
        return "ok"

    await _middleware(monkeypatch).awrap_model_call(request, handler)
    assert seen["req"].system_message.content == "SYSTEM PROMPT"


def test_middleware_tolerates_no_system_message(monkeypatch):
    seen = {}
    request = _FakeRequest("deepseek-v4-flash-fireworks", None)

    _middleware(monkeypatch).wrap_model_call(
        request, lambda req: seen.setdefault("req", req)
    )
    assert seen["req"].system_message is None


# --------------------------------------------------------------------------
# Sweep: agents that don't run the middleware must flatten themselves
# --------------------------------------------------------------------------

def test_every_non_middleware_agent_flattens_its_cached_prompt():
    """Raw StateGraph nodes and sub-agent factories get no DynamicModelMiddleware.

    Any module that builds an Anthropic cache_control system block and does NOT
    run the middleware must route that prompt through system_message_for_model,
    or it 400s the moment the box is on Fireworks/GMI.
    """
    offenders = []
    for path in sorted(AGENTS_DIR.rglob("*.py")):
        src = path.read_text()
        if '"cache_control"' not in src:
            continue
        if "DynamicModelMiddleware" in src:
            continue  # covered by the middleware
        if "system_message_for_model" not in src:
            offenders.append(str(path.relative_to(AGENTS_DIR)))

    assert not offenders, (
        "these agents build a cache_control system prompt but never flatten it "
        f"for OpenAI-compatible providers: {offenders}"
    )
