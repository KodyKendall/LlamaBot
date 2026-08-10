"""Middleware that times every model call and every tool call.

Wired into ``build_leonardo_agent`` so all 11 ``create_agent`` modes get it and
none can silently lose timing (the raw ``StateGraph`` agents — beginner,
ai_builder, plain chat — invoke the model directly and are instrumented at
their call sites instead).

What it measures, and why each piece matters:

* **model wall time** — how long the provider took, end to end.
* **time-to-first-token, per call** — the split that makes tokens/sec honest.
  Prefill grows with prompt size, so on a long thread a perfectly healthy model
  *looks* slower unless TTFT is subtracted out. Captured with a callback handler
  attached to the model for the duration of the call.
* **tool wall time, by name** — separates "the box is slow" from "the LLM is
  slow", which no single turn duration can do.

Design constraints (each pinned by a test in
``app/tests/test_turn_metrics_middleware.py``):

1. **Transparent.** The handler's result is returned untouched.
2. **Never fatal.** Telemetry cannot break a turn: if the model cannot be
   wrapped we lose TTFT for that call and carry on, and a failing call is still
   timed (a slow failure is a performance signal).
3. **Inert without a turn.** Headless runs and tests have no recorder
   installed, so the middleware passes straight through.
"""
import logging
import time

from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler

from app.lib.token_usage import extract_token_usage
from app.lib.turn_metrics import current_turn

logger = logging.getLogger(__name__)


class _FirstTokenTimer(BaseCallbackHandler):
    """Records when the first streamed token arrived for a single model call."""

    def __init__(self, time_fn, started_at):
        self._time_fn = time_fn
        self._started_at = started_at
        self.ttft_ms = None

    def on_llm_new_token(self, *args, **kwargs) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = (self._time_fn() - self._started_at) * 1000.0


def _last_usage(response):
    """Pull token usage off the final message of a ModelResponse, if present."""
    messages = getattr(response, "result", None) or []
    for message in reversed(messages):
        usage = extract_token_usage(message)
        if usage:
            return usage
    return None


class TurnMetricsMiddleware(AgentMiddleware):
    """Times model and tool calls into the active :class:`TurnMetrics`."""

    def __init__(self, *, time_fn=time.monotonic):
        super().__init__()
        # Injectable so tests advance a fake clock instead of sleeping.
        self._time_fn = time_fn

    # -- model calls -------------------------------------------------------

    def _attach_first_token_timer(self, request, started_at):
        """Return ``(request, timer)``; ``timer`` is None if we could not attach.

        Losing TTFT on an exotic model wrapper is acceptable; raising here is
        not — this runs on every single model call in the product.
        """
        timer = _FirstTokenTimer(self._time_fn, started_at)
        try:
            model = request.model.with_config({"callbacks": [timer]})
            return request.override(model=model), timer
        except Exception as exc:  # noqa: BLE001 - telemetry must never be fatal
            logger.debug("turn_metrics: could not attach TTFT callback: %s", exc)
            return request, None

    def _record_model_call(self, turn, started_at, timer, response):
        duration_ms = (self._time_fn() - started_at) * 1000.0
        usage = _last_usage(response) if response is not None else None
        model_name = None
        if response is not None:
            for message in reversed(getattr(response, "result", None) or []):
                model_name = (getattr(message, "response_metadata", None) or {}).get("model_name")
                if model_name:
                    break
        turn.record_model_call(
            duration_ms=duration_ms,
            ttft_ms=timer.ttft_ms if timer else None,
            output_tokens=(usage or {}).get("output_tokens", 0),
            input_tokens=(usage or {}).get("input_tokens", 0),
            model=model_name,
        )

    async def awrap_model_call(self, request, handler):
        turn = current_turn()
        if turn is None:
            return await handler(request)

        started_at = self._time_fn()
        request, timer = self._attach_first_token_timer(request, started_at)
        try:
            response = await handler(request)
        except BaseException:
            # Time the failure too, then let it propagate untouched.
            self._record_model_call(turn, started_at, timer, None)
            raise
        self._record_model_call(turn, started_at, timer, response)
        return response

    def wrap_model_call(self, request, handler):
        turn = current_turn()
        if turn is None:
            return handler(request)

        started_at = self._time_fn()
        request, timer = self._attach_first_token_timer(request, started_at)
        try:
            response = handler(request)
        except BaseException:
            self._record_model_call(turn, started_at, timer, None)
            raise
        self._record_model_call(turn, started_at, timer, response)
        return response

    # -- tool calls --------------------------------------------------------

    @staticmethod
    def _tool_name(request):
        return (getattr(request, "tool_call", None) or {}).get("name")

    async def awrap_tool_call(self, request, handler):
        turn = current_turn()
        if turn is None:
            return await handler(request)

        started_at = self._time_fn()
        try:
            return await handler(request)
        finally:
            turn.record_tool_call(
                name=self._tool_name(request),
                duration_ms=(self._time_fn() - started_at) * 1000.0,
            )

    def wrap_tool_call(self, request, handler):
        turn = current_turn()
        if turn is None:
            return handler(request)

        started_at = self._time_fn()
        try:
            return handler(request)
        finally:
            turn.record_tool_call(
                name=self._tool_name(request),
                duration_ms=(self._time_fn() - started_at) * 1000.0,
            )
