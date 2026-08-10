"""Per-turn performance accounting for a Leonardo chat turn.

Why this exists
---------------
Until now nothing in LlamaBot measured *time*. We shipped rich token telemetry
to the mothership (``report_message`` carries ``token_usage`` including cache
hits) but not one duration, so "Leo is slow" arrived as a support ticket with
no way to tell apart the three very different causes:

* the provider is genuinely decoding slower (tokens/sec down),
* the prompt got huge on a long thread, so time-to-first-token grew with it,
* the box is the bottleneck — tools, the graph loop, checkpointer writes.

A single "turn took 40s" number cannot separate those, so this module records
the turn as *segments* and derives the two rates that matter.

Shape
-----
``TurnMetrics`` is a plain mutable recorder with no I/O, so it is unit-testable
without an event loop or a compiled graph. Instrumentation reaches it through a
``ContextVar``: :func:`start_turn` sets it in the request handler, and the
middleware inside LangGraph's node tasks reads it with :func:`current_turn`.
asyncio copies the context into each spawned task, so a node resolves the var
to the *same* mutable object the handler is holding.

Every accessor is null-safe by design: :func:`current_turn` returns ``None``
when no turn is active (headless executor, background jobs, tests) and callers
skip recording. Telemetry must never raise into an agent turn.

Two deliberate arithmetic choices, both pinned by tests:

* **tokens/sec is measured over decode time, not the whole call.** Time spent
  waiting for the first token is queueing and prefill, not generation; folding
  it in makes a fast model look slow on a long prompt, which is exactly the
  confusion this module exists to remove.
* **overhead clamps at zero.** Tools run concurrently, so summed tool time can
  legitimately exceed the turn's wall clock.
"""
from contextvars import ContextVar
from typing import Optional

# Set per turn by start_turn(). Node tasks spawned by LangGraph inherit a copy
# of the context, so they see the same recorder instance.
_current_turn: ContextVar[Optional["TurnMetrics"]] = ContextVar(
    "llamabot_turn_metrics", default=None
)


class TurnMetrics:
    """Mutable, I/O-free recorder for one chat turn.

    Not synchronized. Recording happens on the event loop, and the only writes
    from a worker thread would be a sync tool's duration — a lost increment
    there costs us one imprecise telemetry sample, which is not worth a lock on
    the hot path.
    """

    def __init__(self, *, thread_id: Optional[str] = None, agent_mode: Optional[str] = None):
        self.thread_id = thread_id
        self.agent_mode = agent_mode

        self._model_calls: list[dict] = []
        self._tool_calls: list[dict] = []
        self._ttft_ms: Optional[float] = None

    # -- recording ---------------------------------------------------------

    def mark_first_token(self, *, elapsed_ms: float) -> None:
        """Record time-to-first-token for the TURN (first call wins).

        This is the user-perceived wait: the gap between hitting send and the
        first character appearing. A turn that opens with a tool call reaches
        its first token several model calls in, which is precisely the case
        worth seeing.
        """
        if self._ttft_ms is None:
            self._ttft_ms = elapsed_ms

    def record_model_call(
        self,
        *,
        duration_ms: float,
        output_tokens: int = 0,
        ttft_ms: Optional[float] = None,
        input_tokens: int = 0,
        model: Optional[str] = None,
    ) -> None:
        self._model_calls.append({
            "duration_ms": duration_ms,
            "ttft_ms": ttft_ms,
            "output_tokens": output_tokens or 0,
            "input_tokens": input_tokens or 0,
            "model": model,
        })

    def record_tool_call(self, *, name: Optional[str], duration_ms: float) -> None:
        self._tool_calls.append({"name": name, "duration_ms": duration_ms})

    # -- derived -----------------------------------------------------------

    def _decode_seconds(self) -> float:
        """Total seconds spent actually generating tokens.

        Per call that is ``duration - ttft``; without a TTFT (non-streaming
        providers) the whole call is the best estimate available.
        """
        total = 0.0
        for call in self._model_calls:
            ttft = call["ttft_ms"]
            decode_ms = call["duration_ms"] - ttft if ttft is not None else call["duration_ms"]
            total += max(decode_ms, 0.0)
        return total / 1000.0

    def last_model_call(self) -> Optional[dict]:
        """Timings for the most recent model call.

        Rides along on the assistant ``report_message`` so every reply in the
        mothership carries the timing of the call that produced it — that is
        the per-message tokens/sec series.
        """
        if not self._model_calls:
            return None
        call = self._model_calls[-1]
        ttft = call["ttft_ms"]
        decode_s = (
            (call["duration_ms"] - ttft if ttft is not None else call["duration_ms"])
        ) / 1000.0
        return {
            "duration_ms": round(call["duration_ms"]),
            "ttft_ms": round(ttft) if ttft is not None else None,
            "tokens_per_second": (
                round(call["output_tokens"] / decode_s, 2) if decode_s > 0 else None
            ),
            "model": call["model"],
        }

    def snapshot(self, *, total_ms: Optional[float] = None) -> dict:
        """Flat, JSON-safe rollup for the mothership.

        ``total_ms`` is the turn's measured wall clock; pass it at end of turn.
        Without it the segment sums are still meaningful and ``overhead_ms`` is
        simply omitted (there is nothing to subtract from).
        """
        model_ms = sum(c["duration_ms"] for c in self._model_calls)
        tool_ms = sum(c["duration_ms"] for c in self._tool_calls)
        decode_s = self._decode_seconds()
        output_tokens = sum(c["output_tokens"] for c in self._model_calls)

        snap = {
            "ttft_ms": round(self._ttft_ms) if self._ttft_ms is not None else None,
            "model_ms": round(model_ms),
            "tool_ms": round(tool_ms),
            "model_calls": len(self._model_calls),
            "tool_calls": len(self._tool_calls),
            "output_tokens": output_tokens,
            # Peak prompt size across the turn — the long-thread signal. Peak,
            # not last, because summarization can compact mid-turn and we want
            # to see the spike that actually cost the user their wait.
            "input_tokens": max((c["input_tokens"] for c in self._model_calls), default=0),
            "tokens_per_second": round(output_tokens / decode_s, 2) if decode_s > 0 else None,
        }

        if self._tool_calls:
            slowest = max(self._tool_calls, key=lambda c: c["duration_ms"])
            snap["slowest_tool"] = {"name": slowest["name"], "ms": round(slowest["duration_ms"])}

        if total_ms is not None:
            snap["total_ms"] = round(total_ms)
            # Time spent neither waiting on a provider nor running a tool: the
            # graph loop, checkpointer writes, serialization, our own overhead.
            snap["overhead_ms"] = round(max(total_ms - model_ms - tool_ms, 0))

        return snap


def start_turn(*, thread_id: Optional[str] = None, agent_mode: Optional[str] = None) -> TurnMetrics:
    """Begin recording a turn and install it for this async context."""
    turn = TurnMetrics(thread_id=thread_id, agent_mode=agent_mode)
    _current_turn.set(turn)
    return turn


def current_turn() -> Optional[TurnMetrics]:
    """The turn being recorded in this context, or ``None`` if there isn't one."""
    return _current_turn.get()
