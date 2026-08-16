"""A turn that runs on a different model than the user picked must say so.

``get_llm`` replaces a policy-disabled model with the box default. That was
silent: the dropdown kept showing the user's choice, every turn ran on the
default, and the only trace was a WARNING in the container log. Live symptom
(2026-08-16, this box): a newly added model was missing from instance.json's
``enabled_models``, so every answer came from Muse while the UI said Nemotron —
including the model answering "who made you?" as the wrong model entirely.

The fix is a ``model_substituted`` frame raised before the run starts, rendered
as a banner above the composer. These tests pin the frame, and pin that the
notice asks the SAME question ``get_llm`` answers — a second copy of the policy
rule would drift and start lying in the other direction.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState

from app.websocket.request_handler import RequestHandler


DISABLED = "nemotron-lightning-30b-fireworks"
ENABLED = "muse-spark-1.2-contributor"


def _connected_websocket():
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


def _handler():
    handler = RequestHandler(MagicMock())
    handler.app.state.mothership_client = None
    return handler


def _frames(websocket):
    return [c.args[0] for c in websocket.send_json.await_args_list]


@pytest.fixture
def only_the_default_enabled(monkeypatch):
    """A box whose allow-list does not carry the model the user selected.

    The META key is part of the fixture, not an assumption about the runner:
    `default_text_model()` degrades Muse to DeepSeek on a box that has no key,
    so without this the substituted model is whatever the environment happens to
    be credentialed for — Muse here, DeepSeek in CI. Pinning the box makes the
    assertions about the notice, not about the runner.
    """
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", ENABLED)
    monkeypatch.setenv("META_API_KEY", "test-key-not-a-real-credential")


@pytest.mark.asyncio
async def test_a_substituted_model_is_reported_to_the_browser(only_the_default_enabled):
    handler = _handler()
    websocket = _connected_websocket()

    await handler._warn_if_model_substituted({"llm_model": DISABLED}, websocket)

    notices = [f for f in _frames(websocket) if f.get("type") == "model_substituted"]
    assert notices, (
        "the turn ran on another model and the browser was told nothing; "
        f"frames: {_frames(websocket)}"
    )
    assert notices[-1]["requested"] == DISABLED
    assert notices[-1]["effective"] == ENABLED


@pytest.mark.asyncio
async def test_no_notice_when_the_selected_model_is_the_one_that_runs(
    only_the_default_enabled,
):
    """The banner must stay rare — a notice on every turn trains users to ignore it."""
    handler = _handler()
    websocket = _connected_websocket()

    await handler._warn_if_model_substituted({"llm_model": ENABLED}, websocket)

    assert not _frames(websocket)


@pytest.mark.asyncio
async def test_no_notice_without_a_model_on_the_frame(only_the_default_enabled):
    """No selection means no expectation to violate — the box default is correct."""
    handler = _handler()
    websocket = _connected_websocket()

    await handler._warn_if_model_substituted({"message": "hi"}, websocket)

    assert not _frames(websocket)


@pytest.mark.asyncio
async def test_the_offline_e2e_model_is_not_reported(monkeypatch):
    """`fake-llm` bypasses the policy gate inside get_llm, so policy's opinion of
    it is not what actually happens — warning about it would fail every mock-LLM
    e2e run with a banner describing a substitution that never occurred."""
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("ENABLED_MODELS", ENABLED)
    monkeypatch.setenv("LLAMABOT_ENABLE_FAKE_LLM", "true")

    handler = _handler()
    websocket = _connected_websocket()

    await handler._warn_if_model_substituted({"llm_model": "fake-llm"}, websocket)

    assert not _frames(websocket)


@pytest.mark.asyncio
async def test_the_notice_never_costs_the_user_their_turn(only_the_default_enabled):
    """Best-effort: a broken socket must not raise out of the turn."""
    handler = _handler()
    websocket = _connected_websocket()
    websocket.send_json = AsyncMock(side_effect=RuntimeError("socket went away"))

    await handler._warn_if_model_substituted({"llm_model": DISABLED}, websocket)


@pytest.mark.asyncio
async def test_the_notice_matches_what_get_llm_actually_builds(only_the_default_enabled):
    """One rule, asked twice. If the notice re-derived the policy it could name a
    model the factory never built — a wrong explanation being worse than none."""
    from app.agents.leonardo.llm_factory import get_llm
    from app.agents.leonardo.model_policy import effective_model

    handler = _handler()
    websocket = _connected_websocket()
    await handler._warn_if_model_substituted({"llm_model": DISABLED}, websocket)
    announced = _frames(websocket)[-1]["effective"]

    assert announced == effective_model(DISABLED)

    with patch(
        "app.agents.leonardo.llm_factory.ChatOpenAI", MagicMock(return_value=object())
    ) as built:
        get_llm(DISABLED)

    assert built.call_args.kwargs["model"] == ENABLED, (
        "get_llm built a different model than the banner promised"
    )


def test_get_llm_reads_the_policy_through_the_shared_helper():
    """Pins the single source of truth structurally: a future edit that inlines
    `is_model_enabled` + `enabled_default_model` back into get_llm reintroduces
    the drift this test exists to prevent."""
    import inspect

    from app.agents.leonardo import llm_factory

    src = inspect.getsource(llm_factory.get_llm)
    assert "effective_model(model_name)" in src
