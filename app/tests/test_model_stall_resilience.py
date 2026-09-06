"""A model that accepts the request and then sends nothing must not eat the turn.

The 2026-08-26 incident (rsb-dev, Muse Spark): the endpoint returned HTTP 200 and
then produced zero streaming chunks. ``langchain_openai``'s *default* 120s
``stream_chunk_timeout`` fired, rung 1 classified it transient — correctly — and
retried the same stalled endpoint. Five attempts x 120s = ~10 minutes of a spinning
shimmer, and the customer's only report was "it runs for like 10 minutes with no
changes". A 503 on the same classifier retries in a second and is invisible; the
zero-chunk variant cost 120s per attempt. Same rung, wildly different user impact.

Three things were missing, and these tests pin all three:

  1. **A wall-clock cap.** The retry *count* was fine; nobody sized the budget
     against an attempt that costs two minutes.
  2. **Rung 2.** There was nowhere else to go — ``with_fallbacks`` appears nowhere
     in ``app/``, so Leo retried the one endpoint that was down.
  3. **Any sign of life.** A retry was completely silent.

Assertions here are about structure and bounds, never about wording or timing on
the runner: real sleeps are driven to tiny monkeypatched budgets so the mechanism
is what is measured, and the *sizing* is asserted as arithmetic.
"""

import asyncio
import time

import pytest

from app.agents.leonardo.rails_agent import middleware as mw


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _Model:
    """Enough of a chat model that _override/bind_tools downstream still work."""

    def __init__(self, name):
        self.name = name

    def bind_tools(self, *a, **k):
        return self


class _Req:
    """Records which model each override selected, in order."""

    def __init__(self, model_name="muse-spark-1.2-contributor", messages=None):
        self.state = {"llm_model": model_name}
        self.model = None
        self.messages = messages or []
        self.selected = []

    def override(self, **kw):
        model = kw.get("model")
        if model is not None:
            self.model = model
            self.selected.append(getattr(model, "name", model))
        return self


class _Image:
    """A user message carrying an image block, the way the websocket builds one."""

    content = [
        {"type": "text", "text": "what is in this screenshot?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


def _stall(model="muse-spark-1.2-contributor"):
    """The exact exception langchain_openai raises for a zero-chunk stream.

    A ``TimeoutError`` subclass, so ``is_transient_error`` classifies it transient
    — which is correct and deliberately not what this ticket changes.
    """
    return TimeoutError(
        f"No streaming chunk received for 25.0s (model={model}, chunks_received=0)."
    )


@pytest.fixture
def no_backoff(monkeypatch):
    """Retry immediately — the backoff is not what any of this is measuring.

    Zeroes the *delay* rather than patching ``time.sleep``/``asyncio.sleep``.
    Those are global, so patching them also silences the deliberate stall inside
    the test handlers, and the clock the budget reads never advances.
    """
    monkeypatch.setattr(mw, "_model_retry_delay", lambda attempt: 0)


@pytest.fixture
def models_build(monkeypatch):
    monkeypatch.setattr(mw, "get_llm", lambda name: _Model(name))


@pytest.fixture
def no_fallback(monkeypatch):
    """A box where rung 2 has nowhere to go, so rung 1's bound is what shows."""
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Acceptance 1 — a stalled model is abandoned on a wall clock, not a counter
# ---------------------------------------------------------------------------

def test_the_retry_budget_is_sized_against_a_stalling_attempt():
    """The arithmetic that was never done.

    ``_MODEL_RETRY_MAX_ATTEMPTS`` alone bounds nothing useful: multiplied by the
    cost of a *stalling* attempt it is ten minutes. The budget has to be the thing
    that binds, and the worst case (budget already spent, one attempt still in
    flight) has to land under the 90s the incident review asked for.
    """
    from app.agents.leonardo.llm_factory import stream_chunk_timeout_s
    from app.agents.leonardo.resilience import (
        _MODEL_RETRY_MAX_ATTEMPTS,
        _MODEL_RETRY_MAX_TOTAL_SECONDS,
    )

    attempt_cost = stream_chunk_timeout_s()
    assert _MODEL_RETRY_MAX_ATTEMPTS * attempt_cost > _MODEL_RETRY_MAX_TOTAL_SECONDS, (
        "the attempt counter still bounds the loop before the clock does, which is "
        "the bug: five stalling attempts is ~10 minutes of silence"
    )
    assert _MODEL_RETRY_MAX_TOTAL_SECONDS + attempt_cost <= 90, (
        "worst case is the budget plus the attempt that was in flight when it ran "
        "out; that total is what the user actually sits through"
    )


def test_the_chunk_timeout_is_chosen_not_inherited():
    """120s was ``langchain_openai``'s default, not a decision anyone made.

    Not disabled, either — a disabled chunk timeout is how a turn hangs forever.
    """
    from app.agents.leonardo.llm_factory import stream_chunk_timeout_s

    value = stream_chunk_timeout_s()
    assert value is not None and value > 0, "a disabled chunk timeout hangs forever"
    assert value <= 30, "no healthy first chunk takes this long; 120s is the bug"


def test_the_chunk_timeout_is_overridable(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "7.5")
    from app.agents.leonardo.llm_factory import stream_chunk_timeout_s

    assert stream_chunk_timeout_s() == 7.5


def test_every_built_client_carries_the_chunk_timeout(monkeypatch):
    """Applied centrally, and NOT left to the environment.

    ``langchain_openai`` reads the same env var itself, so "just set it in the
    container" looks like a fix — until you meet a box whose .env nobody edited,
    which is every box in the fleet. With the var unset the library's answer is
    120.0; ours has to be the one that ships. There are 11 ``ChatOpenAI(`` call
    sites in llm_factory, so this is applied in one place, not eleven.
    """
    monkeypatch.delenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-credential")
    from app.agents.leonardo.llm_factory import get_llm, stream_chunk_timeout_s

    client = get_llm("deepseek-v4-flash")
    assert client.stream_chunk_timeout == stream_chunk_timeout_s()
    assert client.stream_chunk_timeout != 120.0, "still the inherited default"


def test_an_operator_override_reaches_the_client(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "9")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-credential")
    from app.agents.leonardo.llm_factory import get_llm

    assert get_llm("deepseek-v4-flash").stream_chunk_timeout == 9.0


def test_a_stalling_model_is_abandoned_before_the_attempt_cap(
    monkeypatch, models_build, no_fallback
):
    """The behavioural half: the loop stops on the clock, with attempts to spare.

    Real time, a tiny budget — the mechanism is what is under test, and the size
    of the real budget is pinned arithmetically above.
    """
    monkeypatch.setattr(mw, "_MODEL_RETRY_MAX_TOTAL_SECONDS", 0.05)
    monkeypatch.setattr(mw, "_model_retry_delay", lambda attempt: 0)

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        time.sleep(0.03)        # a stalling attempt: it costs real wall clock
        raise _stall()

    with pytest.raises(TimeoutError):
        mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler)

    assert calls["n"] < mw._MODEL_RETRY_MAX_ATTEMPTS, (
        f"burned the whole attempt budget ({calls['n']} calls) — the wall clock "
        "never got a vote, which is the ~600s incident"
    )


def test_a_stalling_model_is_abandoned_before_the_attempt_cap_async(
    monkeypatch, models_build, no_fallback
):
    """Same bound on the websocket path, which is where customers actually are."""
    monkeypatch.setattr(mw, "_MODEL_RETRY_MAX_TOTAL_SECONDS", 0.05)
    monkeypatch.setattr(mw, "_model_retry_delay", lambda attempt: 0)

    calls = {"n": 0}

    async def handler(req):
        calls["n"] += 1
        await asyncio.sleep(0.03)
        raise _stall()

    with pytest.raises(TimeoutError):
        asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(_Req(), handler))

    assert calls["n"] < mw._MODEL_RETRY_MAX_ATTEMPTS


def test_the_raw_node_ladder_is_bounded_too(monkeypatch):
    """``invoke_with_transient_retry`` is the same rung for agents that never
    touch the middleware (beginner, ai_builder). It had the same missing cap."""
    from app.agents.leonardo import resilience

    monkeypatch.setattr(resilience, "_MODEL_RETRY_MAX_TOTAL_SECONDS", 0.05)
    monkeypatch.setattr(resilience, "_model_retry_delay", lambda attempt: 0)

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        time.sleep(0.03)
        raise _stall()

    with pytest.raises(TimeoutError):
        resilience.invoke_with_transient_retry(fn, label="test")

    assert calls["n"] < resilience._MODEL_RETRY_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Acceptance 2 + 3 — rung 2, resolved from policy
# ---------------------------------------------------------------------------

def test_the_turn_completes_on_the_fallback_model(monkeypatch, models_build, no_backoff):
    """The whole point: a stalled primary must not end the turn."""
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")

    req = _Req("muse-spark-1.2-contributor")

    def handler(r):
        if r.model.name == "muse-spark-1.2-contributor":
            raise _stall()
        return "answered"

    assert mw.DynamicModelMiddleware().wrap_model_call(req, handler) == "answered"
    assert req.selected[0] == "muse-spark-1.2-contributor"
    assert req.selected[-1] == "deepseek-v4-flash"


def test_the_turn_completes_on_the_fallback_model_async(
    monkeypatch, models_build, no_backoff
):
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")

    req = _Req("muse-spark-1.2-contributor")

    async def handler(r):
        if r.model.name == "muse-spark-1.2-contributor":
            raise _stall()
        return "answered"

    out = asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(req, handler))
    assert out == "answered"
    assert req.selected[-1] == "deepseek-v4-flash"


def test_the_fallback_is_tried_once_and_then_the_turn_fails(
    monkeypatch, models_build, no_backoff
):
    """Escalation stays inside one turn and does not walk the whole registry.

    Without a cap the resolver hands back the box default, whose own fallback is
    the primary again — an endless loop between two dead endpoints.
    """
    monkeypatch.setattr(mw, "_MODEL_RETRY_MAX_TOTAL_SECONDS", 0.01)
    seen = []

    def _resolver(primary, **kw):
        seen.append(primary)
        return {"muse-spark-1.2-contributor": "deepseek-v4-flash"}.get(
            primary, "muse-spark-1.2-contributor"
        )

    monkeypatch.setattr(mw, "fallback_model", _resolver)

    def handler(r):
        raise _stall()

    with pytest.raises(TimeoutError):
        mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler)

    assert len(seen) == 1, f"escalated more than one rung: {seen}"


def test_a_deterministic_error_never_reaches_the_fallback(
    monkeypatch, models_build, no_backoff
):
    """Rung 2 is for a provider that is down, not for a request we got wrong.

    A 400 fails identically everywhere; sending it to a second provider only buys
    a second 400 and another 25 seconds. ``is_transient_error`` stays untouched —
    this is the behaviour that depends on it.
    """
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        raise TypeError("unexpected keyword argument 'cache_control'")

    with pytest.raises(TypeError):
        mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler)

    assert calls["n"] == 1


def test_the_middleware_asks_policy_for_the_fallback():
    """Structural guard, same one ``test_no_agent_hardcodes_a_fallback_model``
    exists for: a literal model id here reintroduces the 0.7.0 bug where the
    fleet default was silently ignored."""
    import inspect

    src = inspect.getsource(mw)
    assert "fallback_model(" in src
    assert "from app.agents.leonardo.model_policy import" in src or hasattr(
        mw, "fallback_model"
    )


@pytest.fixture
def a_two_model_box(monkeypatch):
    """A box that can build both fail-open models and has no operator overrides.

    Every knob is pinned, not merely deleted: MODEL_SWITCHING_ALLOWED in
    particular is left set by other suites, and when it is off the policy pins
    the box to its single default — which reads here as "the fallback resolver is
    broken" rather than "the environment leaked".
    """
    from app.agents.leonardo import model_policy

    monkeypatch.setenv("META_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.delenv("ENABLED_MODELS", raising=False)
    monkeypatch.delenv("DISABLED_MODELS", raising=False)
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    return model_policy


def test_the_fallback_is_a_different_enabled_model(a_two_model_box):
    model_policy = a_two_model_box

    chosen = model_policy.fallback_model("muse-spark-1.2-contributor")
    assert chosen == "deepseek-v4-flash"
    assert model_policy.is_model_enabled(chosen)


def test_the_fallback_is_never_the_model_that_just_stalled(monkeypatch):
    from app.agents.leonardo import model_policy

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("ENABLED_MODELS", raising=False)
    monkeypatch.delenv("DISABLED_MODELS", raising=False)
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)

    # DeepSeek is the box default here, so the naive "fall back to the default"
    # answer would hand back the endpoint that just failed.
    assert model_policy.fallback_model("deepseek-v4-flash") != "deepseek-v4-flash"


def test_there_is_no_fallback_when_policy_leaves_nothing_to_fall_back_to(monkeypatch):
    """A box pinned to one model has no rung 2, and must say so rather than
    inventing a model ``get_llm`` would only substitute straight back."""
    from app.agents.leonardo import model_policy

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "false")
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)

    assert model_policy.fallback_model("deepseek-v4-flash") is None


def test_an_image_turn_never_falls_back_to_a_model_that_cannot_see(monkeypatch):
    """The vision routing rule survives the fallback. Falling back to a blind
    model answers the question about the screenshot without the screenshot."""
    from app.agents.leonardo import model_capabilities, model_policy

    monkeypatch.setenv("META_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.delenv("ENABLED_MODELS", raising=False)
    monkeypatch.delenv("DISABLED_MODELS", raising=False)
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)

    chosen = model_policy.fallback_model(
        "muse-spark-1.2-contributor", needs_vision=True
    )
    if chosen is not None:
        assert model_capabilities.get_model_capabilities(chosen).get("images") is True


def test_an_image_request_asks_for_a_vision_fallback(monkeypatch, models_build, no_backoff):
    """And the middleware actually notices the image on the request."""
    asked = {}

    def _resolver(primary, **kw):
        asked.update(kw)
        return None

    monkeypatch.setattr(mw, "fallback_model", _resolver)

    def handler(r):
        raise _stall()

    with pytest.raises(TimeoutError):
        mw.DynamicModelMiddleware().wrap_model_call(
            _Req(messages=[_Image()]), handler
        )

    assert asked.get("needs_vision") is True


def test_a_real_langchain_image_message_is_recognised():
    """Not just the hand-rolled double above.

    LangChain normalizes ``image_url`` to ``image`` in ``content_blocks``, so a
    detector that only knows one of the two spellings silently answers "no image
    here" and lets rung 2 pick a model that cannot see.
    """
    from langchain_core.messages import HumanMessage

    request = _Req(
        messages=[
            HumanMessage(
                content=[
                    {"type": "text", "text": "what is in this screenshot?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ]
            )
        ]
    )
    assert mw._request_carries_an_image(request) is True
    assert mw._request_carries_an_image(_Req(messages=[HumanMessage("plain text")])) is False


def test_a_text_request_does_not_ask_for_a_vision_fallback(
    monkeypatch, models_build, no_backoff
):
    asked = {}

    def _resolver(primary, **kw):
        asked.update(kw)
        return None

    monkeypatch.setattr(mw, "fallback_model", _resolver)

    def handler(r):
        raise _stall()

    with pytest.raises(TimeoutError):
        mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler)

    assert asked.get("needs_vision") is False


# ---------------------------------------------------------------------------
# Acceptance 4 — a retry the user can feel is announced; a fast one is not
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_notices(monkeypatch):
    """Install a notice channel for the current context and collect its frames."""
    from app.lib import turn_notices

    frames = []

    async def _send(frame):
        frames.append(frame)

    turn_notices.start_turn_notices(_send)
    yield frames
    turn_notices.clear_turn_notices()


def _thinking(frames):
    out = []
    for f in frames:
        for block in f.get("thinking") or []:
            out.append(block.get("thinking", ""))
    return out


def test_a_fast_transient_retry_says_nothing(monkeypatch, models_build, no_backoff):
    """A 503 retries in about a second and is invisible. A notice on every one of
    those trains users to ignore the notice that matters."""
    from app.lib import turn_notices

    frames = []
    turn_notices.start_turn_notices(lambda f: frames.append(f))
    try:
        calls = {"n": 0}

        def handler(r):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("503 service_overloaded")
            return "answered"

        assert mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler) == "answered"
    finally:
        turn_notices.clear_turn_notices()

    assert not frames, f"a one-second blip was announced: {frames}"


def test_a_retry_the_user_can_feel_is_announced(monkeypatch, models_build, no_backoff):
    """Past the threshold the shimmer has been spinning long enough that silence
    is indistinguishable from a frozen agent."""
    monkeypatch.setattr(mw, "_RETRY_NOTICE_AFTER_SECONDS", 0.0)

    frames = []

    async def _send(frame):
        frames.append(frame)

    from app.lib import turn_notices

    turn_notices.start_turn_notices(_send)
    try:
        calls = {"n": 0}

        async def handler(r):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _stall()
            return "answered"

        out = asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(_Req(), handler))
    finally:
        turn_notices.clear_turn_notices()

    assert out == "answered"
    said = " ".join(_thinking(frames)).lower()
    assert "retry" in said, f"nothing told the user a retry was happening: {frames}"
    assert all(f.get("type") == "AIMessageChunk" for f in frames), (
        "the notice must ride the shimmer frame, not land in the transcript"
    )


def test_the_notice_never_costs_the_user_their_turn(monkeypatch, models_build, no_backoff):
    """Best effort, like every other status frame: a dead socket is not a reason
    to fail the turn the notice was describing."""
    monkeypatch.setattr(mw, "_RETRY_NOTICE_AFTER_SECONDS", 0.0)

    async def _boom(frame):
        raise RuntimeError("socket went away")

    from app.lib import turn_notices

    turn_notices.start_turn_notices(_boom)
    try:
        calls = {"n": 0}

        async def handler(r):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _stall()
            return "answered"

        out = asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(_Req(), handler))
    finally:
        turn_notices.clear_turn_notices()

    assert out == "answered"


def test_a_turn_with_no_channel_installed_still_runs(monkeypatch, models_build, no_backoff):
    """Every non-websocket caller (cron, tests, sub-agents) has no sink at all."""
    from app.lib import turn_notices

    turn_notices.clear_turn_notices()
    monkeypatch.setattr(mw, "_RETRY_NOTICE_AFTER_SECONDS", 0.0)

    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _stall()
        return "answered"

    assert mw.DynamicModelMiddleware().wrap_model_call(_Req(), handler) == "answered"


# ---------------------------------------------------------------------------
# Acceptance 5 — the fallback rides the frame that already exists
# ---------------------------------------------------------------------------

def test_the_fallback_is_announced_on_the_model_substituted_frame(
    monkeypatch, models_build, no_backoff
):
    """Not a new frame type: ``model_substituted`` is already rendered end to end.
    It only ever fired for *policy* substitution, before the run started."""
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")

    frames = []

    async def _send(frame):
        frames.append(frame)

    from app.lib import turn_notices

    turn_notices.start_turn_notices(_send)
    try:
        req = _Req("muse-spark-1.2-contributor")

        async def handler(r):
            if r.model.name == "muse-spark-1.2-contributor":
                raise _stall()
            return "answered"

        asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(req, handler))
    finally:
        turn_notices.clear_turn_notices()

    swaps = [f for f in frames if f.get("type") == "model_substituted"]
    assert swaps, f"the turn changed model and the browser was told nothing: {frames}"
    assert swaps[-1]["requested"] == "muse-spark-1.2-contributor"
    assert swaps[-1]["effective"] == "deepseek-v4-flash"
    assert swaps[-1]["reason"] == "fallback", (
        "policy substitution and a mid-turn failure fallback need different "
        "wording in the banner, so the frame has to say which one this is"
    )


@pytest.mark.asyncio
async def test_policy_substitution_still_labels_itself():
    """The pre-existing sender gains the same field, so the browser never has to
    guess what an unlabelled frame meant."""
    from unittest.mock import AsyncMock, MagicMock

    from starlette.websockets import WebSocketState

    from app.websocket.request_handler import RequestHandler

    import os

    os.environ["MODEL_SWITCHING_ALLOWED"] = "true"
    os.environ["ENABLED_MODELS"] = "muse-spark-1.2-contributor"
    os.environ["META_API_KEY"] = "test-key-not-a-real-credential"
    try:
        handler = RequestHandler(MagicMock())
        handler.app.state.mothership_client = None
        ws = MagicMock()
        ws.client_state = WebSocketState.CONNECTED
        ws.application_state = WebSocketState.CONNECTED
        ws.send_json = AsyncMock()

        await handler._warn_if_model_substituted(
            {"llm_model": "nemotron-lightning-30b-fireworks"}, ws
        )
        frame = ws.send_json.await_args_list[-1].args[0]
    finally:
        for key in ("MODEL_SWITCHING_ALLOWED", "ENABLED_MODELS", "META_API_KEY"):
            os.environ.pop(key, None)

    assert frame["type"] == "model_substituted"
    assert frame["reason"] == "policy"


# ---------------------------------------------------------------------------
# Acceptance 6 — a MID-STREAM stall skips rung 1 entirely (0.7.7)
# ---------------------------------------------------------------------------
#
# The tests above are the chunks_received == 0 case: the provider accepted the
# request and produced nothing, so rung 1 is right — a retry costs one timeout
# and may well land on a healthy replica.
#
# This is the other variant, and it is a different animal. On 2026-08-31 the
# fleet default moved to glm-5.3-flash-zai, pinned to z-ai/fp8 with
# allow_fallbacks: false. Turns started dying with chunks_received=478: the
# provider accepted the request, did the expensive work, streamed most of an
# answer, and then went quiet. 38 rows across 6 customer boxes in 7 days, none
# before 08-31. The customer's report was "they insist they're still making
# progress but the task step hasn't progressed for hours".
#
# Retrying THAT is strictly worse than moving. The work is already thrown away,
# the pin guarantees the retry lands on the same sick replica, and each attempt
# costs a whole generation rather than a fast 503 — so rung 1's 60s budget buys
# nothing at all before rung 2 finally gets a turn.

def _midstream_stall(model="glm-5.3-flash-zai", chunks=478):
    """A stream that produced content and then stopped.

    Built from the real ``langchain_openai`` class, not a stand-in: the whole
    fix rests on that exception being a ``TimeoutError`` that carries
    ``chunks_received``, and a library change to either is a change this must
    notice.
    """
    from langchain_openai.chat_models._client_utils import StreamChunkTimeoutError

    return StreamChunkTimeoutError(25.0, model_name=model, chunks_received=chunks)


def test_the_real_exception_carries_the_chunk_count():
    """The attribute the fix reads, on the class the provider actually raises.

    Its own docstring promises these "so diagnostic code doesn't need to regex
    the message" — this is that diagnostic code taking it at its word.
    """
    exc = _midstream_stall()

    assert isinstance(exc, TimeoutError), (
        "if this stops holding, is_transient_error stops seeing it at all"
    )
    assert exc.chunks_received == 478
    assert _midstream_stall(chunks=0).chunks_received == 0


def test_a_midstream_stall_is_told_apart_from_a_zero_chunk_one():
    from app.agents.leonardo.resilience import is_midstream_stall, is_transient_error

    assert is_midstream_stall(_midstream_stall(chunks=478)) is True
    assert is_midstream_stall(_midstream_stall(chunks=0)) is False

    # Untouched: a zero-chunk stall keeps its rung-1 retry, which is the
    # 2026-08-26 Muse fix and still correct.
    assert is_transient_error(_midstream_stall(chunks=0)) is True

    # Not every timeout is a stall, and not every object with a chunk count is
    # a timeout.
    assert is_midstream_stall(TimeoutError("read timed out")) is False
    assert is_midstream_stall(ValueError("chunks_received=478")) is False


def test_a_midstream_stall_moves_to_rung_2_without_retrying(
    monkeypatch, models_build, no_backoff
):
    """The behavioural claim: one call on the primary, then a different model."""
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")

    req = _Req("glm-5.3-flash-zai")
    calls = []

    def handler(r):
        calls.append(r.model.name)
        if r.model.name == "glm-5.3-flash-zai":
            raise _midstream_stall()
        return "answered"

    assert mw.DynamicModelMiddleware().wrap_model_call(req, handler) == "answered"
    assert calls == ["glm-5.3-flash-zai", "deepseek-v4-flash"], (
        f"retried the replica that already threw the work away: {calls}"
    )


def test_a_midstream_stall_moves_to_rung_2_without_retrying_async(
    monkeypatch, models_build, no_backoff
):
    """The websocket path carries its own copy of the ladder; it must not drift."""
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")

    req = _Req("glm-5.3-flash-zai")
    calls = []

    async def handler(r):
        calls.append(r.model.name)
        if r.model.name == "glm-5.3-flash-zai":
            raise _midstream_stall()
        return "answered"

    out = asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(req, handler))
    assert out == "answered"
    assert calls == ["glm-5.3-flash-zai", "deepseek-v4-flash"], (
        f"async path still retries the stalled replica: {calls}"
    )


def test_a_zero_chunk_stall_still_retries_the_same_model(
    monkeypatch, models_build, no_backoff
):
    """Regression guard on the 2026-08-26 fix.

    The narrow reading of this ticket — "stalls skip rung 1" — would delete the
    retry that incident bought. It must stay: a provider that produced nothing
    has thrown nothing away, and the next attempt often lands on a healthy
    replica.
    """
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")

    req = _Req("glm-5.3-flash-zai")
    calls = []

    def handler(r):
        calls.append(r.model.name)
        if len(calls) == 1:
            raise _midstream_stall(chunks=0)
        return "answered"

    assert mw.DynamicModelMiddleware().wrap_model_call(req, handler) == "answered"
    assert calls == ["glm-5.3-flash-zai", "glm-5.3-flash-zai"], (
        f"a zero-chunk stall lost its rung-1 retry: {calls}"
    )


def test_a_zero_chunk_stall_still_retries_the_same_model_async(
    monkeypatch, models_build, no_backoff
):
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")

    req = _Req("glm-5.3-flash-zai")
    calls = []

    async def handler(r):
        calls.append(r.model.name)
        if len(calls) == 1:
            raise _midstream_stall(chunks=0)
        return "answered"

    out = asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(req, handler))
    assert out == "answered"
    assert calls == ["glm-5.3-flash-zai", "glm-5.3-flash-zai"]


def test_a_midstream_stall_does_not_mark_the_model_gone(
    monkeypatch, models_build, no_backoff
):
    """The model is fine; this replica is sick.

    ``mark_model_gone`` carries a 15-minute TTL and steers every later turn on
    the box away. That is right for a 404 (the id no longer exists anywhere) and
    wrong here — the next turn may well be routed to a healthy replica, and
    banning the box default over one bad stream is a self-inflicted outage.
    """
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")
    marked = []
    monkeypatch.setattr(
        mw.model_health, "mark_model_gone", lambda name: marked.append(name)
    )

    def handler(r):
        if r.model.name == "glm-5.3-flash-zai":
            raise _midstream_stall()
        return "answered"

    mw.DynamicModelMiddleware().wrap_model_call(_Req("glm-5.3-flash-zai"), handler)
    assert marked == [], f"banned a model that is merely serving one bad replica: {marked}"


def test_a_midstream_stall_does_not_mark_the_model_gone_async(
    monkeypatch, models_build, no_backoff
):
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: "deepseek-v4-flash")
    marked = []
    monkeypatch.setattr(
        mw.model_health, "mark_model_gone", lambda name: marked.append(name)
    )

    async def handler(r):
        if r.model.name == "glm-5.3-flash-zai":
            raise _midstream_stall()
        return "answered"

    asyncio.run(
        mw.DynamicModelMiddleware().awrap_model_call(_Req("glm-5.3-flash-zai"), handler)
    )
    assert marked == []


def test_a_midstream_stall_with_nowhere_to_go_surfaces_the_error(
    monkeypatch, models_build, no_fallback, no_backoff
):
    """No new terminal path. A box pinned to one model still gets the raise —
    and gets it on the FIRST failure rather than 60s later."""
    calls = {"n": 0}

    def handler(r):
        calls["n"] += 1
        raise _midstream_stall()

    with pytest.raises(TimeoutError):
        mw.DynamicModelMiddleware().wrap_model_call(_Req("glm-5.3-flash-zai"), handler)

    assert calls["n"] == 1, (
        f"spent rung 1's budget on a replica that had already given up: {calls['n']}"
    )


def test_a_midstream_stall_with_nowhere_to_go_surfaces_the_error_async(
    monkeypatch, models_build, no_fallback, no_backoff
):
    calls = {"n": 0}

    async def handler(r):
        calls["n"] += 1
        raise _midstream_stall()

    with pytest.raises(TimeoutError):
        asyncio.run(
            mw.DynamicModelMiddleware().awrap_model_call(_Req("glm-5.3-flash-zai"), handler)
        )

    assert calls["n"] == 1
