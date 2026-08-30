"""Model-agnostic resilience primitives for the LLM call path.

This is the shared, provider-neutral policy for the resilience ladder (see
docs/dev/error_telemetry.md). It answers one question: *is this exception worth
retrying?* — without knowing or caring which model/provider raised it.

Design contract (locked by tests/test_resilience_transient_retry.py):

  * Transient infra failures (rate limits, timeouts, connection drops, 5xx) are
    retryable — a retry genuinely helps.
  * Deterministic request/config errors (bad kwarg -> TypeError; invalid content
    -> 400) are NOT retryable — retrying fails identically and just delays the
    fallback/floor that could actually recover. The two production bugs that
    motivated this (cache_control TypeError, image_url 400) MUST classify as
    non-transient.

Provider SDK imports are individually guarded so a missing optional dependency
can never break graph compilation (fleet-wide compile guard).
"""

import logging
import time

logger = logging.getLogger(__name__)

# HTTP status codes we treat as transient (safe to retry). Deliberately excludes
# 4xx client errors (400/401/403/404/422) — those are deterministic: our request
# is malformed/unauthorized/not-found and will fail identically on retry.
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def transient_exception_types() -> tuple[type, ...]:
    """Return the tuple of exception *types* considered transient/retryable.

    Suitable for ``Runnable.with_retry(retry_if_exception_type=...)``. Only
    narrow, unambiguously-transient classes are included — never a provider's
    base ``APIError``/``APIStatusError`` (those sweep in 400s/422s).
    """
    types: list[type] = [TimeoutError, ConnectionError]

    try:
        import httpx
        types += [
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
            # Mid-stream socket read/write failures (mapped from httpcore.Read/
            # WriteError). ReadError has an empty str(e); it kills a turn when the
            # peer drops the connection while we're streaming the response. Same
            # transient family as the timeouts above — a retry genuinely helps.
            httpx.ReadError,
            httpx.WriteError,
        ]
    except Exception:  # pragma: no cover - httpx is always present today
        pass

    # openai covers OpenAI, DeepSeek and every OpenAI-compatible client.
    try:
        import openai
        types += [
            openai.RateLimitError,        # 429
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,   # 5xx
        ]
    except Exception:
        pass

    try:
        import anthropic
        types += [
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ]
    except Exception:
        pass

    try:
        from google.api_core.exceptions import (
            ResourceExhausted,      # preserves the pre-existing Google retry behavior
            ServiceUnavailable,
            DeadlineExceeded,
            InternalServerError as GoogleInternalServerError,
        )
        types += [
            ResourceExhausted,
            ServiceUnavailable,
            DeadlineExceeded,
            GoogleInternalServerError,
        ]
    except Exception:
        pass

    # Preserve order, drop duplicates.
    return tuple(dict.fromkeys(types))


def _status_code_of(exc: BaseException):
    """Best-effort extraction of an HTTP status code from any provider error."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    # OpenRouter reports an upstream provider failure as a plain ValueError whose
    # single arg is a dict — no `.status_code`, no `.response`, so both lookups
    # above miss and a 502 saying "Retry after 2s" was classed non-transient.
    # Measured on the real endpoint: this is the dominant failure mode of the
    # routed path, not a corner case.
    #
    # Deliberately narrow — only a dict arg with an INT under a status-ish key.
    # `bool` is excluded because it is an int subclass, and a non-int code (a
    # provider string like "rate_limited") must not be mistaken for a status.
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, dict):
            for key in ("code", "status_code", "status"):
                value = arg.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    return None


def is_transient_error(exc: BaseException) -> bool:
    """Model-agnostic predicate: is this exception worth retrying?

    Two independent signals, either sufficient:
      1. the exception is an instance of a known-transient provider type, or
      2. it carries a transient HTTP status code (429/5xx/408).

    Everything else — including bare ``Exception`` and all deterministic 4xx —
    is non-transient, so it flows on to the fallback/floor rung instead of being
    pointlessly retried.
    """
    if isinstance(exc, transient_exception_types()):
        return True
    code = _status_code_of(exc)
    if code is not None:
        return code in _TRANSIENT_STATUS
    return False


# =============================================================================
# Rung 1 retry mechanics (shared by every call path, not just the middleware)
# =============================================================================
#
# The retry *policy* (how many attempts, how long to back off) lives here next to
# the transient/deterministic classifier so there is exactly one source of truth.
# DynamicModelMiddleware.wrap_model_call runs its own inline loop over the handler
# (it also does model selection); raw StateGraph nodes that call the model
# directly — and therefore never touch the middleware — use invoke_with_transient
# _retry below. Both share these constants so their behavior can't drift apart.
_MODEL_RETRY_MAX_ATTEMPTS = 5          # initial call + up to 4 retries
_MODEL_RETRY_BASE_DELAY = 0.5          # seconds
_MODEL_RETRY_MAX_DELAY = 8.0           # seconds

# Total wall clock the whole rung may spend on one model before handing over to
# rung 2, regardless of attempts remaining.
#
# The attempt count was never the bug. The backoff (0.5-8s) was sized against a
# 503, where the failing attempt itself costs about a second — five of those is
# a few seconds and nobody notices. It was never sized against an attempt that
# costs a full chunk timeout: on 2026-08-26 a Muse Spark endpoint returned HTTP
# 200 and streamed nothing, so each attempt burned the library's inherited 120s
# default and five of them was ~10 minutes of silence. The customer's report was
# "it runs for like 10 minutes with no changes".
#
# 60, not 90, because what the user sits through is this budget PLUS the attempt
# that was still in flight when it ran out: 60 + a 25s chunk timeout = 85s, which
# is the "abandoned in under 90 seconds" the incident review asked for. Raising
# this to 90 quietly buys a 115s worst case.
_MODEL_RETRY_MAX_TOTAL_SECONDS = 60.0

# How long the user must already have been waiting before a retry is worth
# telling them about. Below this the retry is genuinely invisible — a 503 comes
# back in a second or two — and a notice on every one of those trains people to
# ignore the notice that matters. Above it, the shimmer has been spinning long
# enough that silence is indistinguishable from a frozen agent.
_RETRY_NOTICE_AFTER_SECONDS = 10.0


def _model_retry_delay(attempt: int) -> float:
    """Exponential backoff (capped) for the Nth failed attempt (1-based)."""
    return min(_MODEL_RETRY_BASE_DELAY * (2 ** (attempt - 1)), _MODEL_RETRY_MAX_DELAY)


def retry_notice_text(attempt: int, max_attempts: int = None) -> str:
    """What the shimmer says while the ladder is working.

    Names the situation the user is actually in ("not responding"), not the
    mechanism. Retries are counted from the user's point of view — attempt 1 is
    the first *retry*, because the failed original call is not something they
    were ever shown.
    """
    if max_attempts is None:
        max_attempts = _MODEL_RETRY_MAX_ATTEMPTS
    return (
        f"Model is not responding. Retrying ({attempt} of {max_attempts - 1})…"
    )


def _notify_slow_retry(label: str, attempt: int, elapsed: float) -> bool:
    """Tell the user about a retry they have already felt. Sync path.

    Below :data:`_RETRY_NOTICE_AFTER_SECONDS` this does nothing on purpose — see
    the constant. Best effort in every direction: no channel installed (cron, a
    sub-agent, a test) is the normal case, not an error.
    """
    if elapsed < _RETRY_NOTICE_AFTER_SECONDS:
        return False
    try:
        from app.lib.turn_notices import notify, thinking_frame

        return notify(thinking_frame(retry_notice_text(attempt)))
    except Exception as e:  # noqa: BLE001 - a notice never fails a turn
        logger.debug("could not announce a retry on %s: %s", label, e)
        return False


def retry_budget_spent(started_at: float, budget: float = None) -> bool:
    """True once this model has had all the wall clock the rung will give it.

    ``started_at`` is a ``time.monotonic()`` stamp from when the first attempt
    began. Checked *before* sleeping and retrying, never as a deadline on an
    in-flight call — cancelling a request mid-flight would lose a response that
    may still be coming.
    """
    if budget is None:
        budget = _MODEL_RETRY_MAX_TOTAL_SECONDS
    return (time.monotonic() - started_at) >= budget


def _record_raw_model_call(turn, started_at: float, response) -> None:
    """Record a direct (non-middleware) model invocation into the active turn.

    No TTFT: these call sites use blocking ``.invoke()``, so there is no first
    token to observe. tokens/sec for these calls therefore includes prefill —
    ``input_tokens`` travels alongside so the two can still be separated
    downstream. Never raises; telemetry must not break a raw node.
    """
    if turn is None:
        return
    try:
        from app.lib.token_usage import extract_token_usage

        usage = extract_token_usage(response) if response is not None else None
        turn.record_model_call(
            duration_ms=(time.monotonic() - started_at) * 1000.0,
            output_tokens=(usage or {}).get("output_tokens", 0),
            input_tokens=(usage or {}).get("input_tokens", 0),
            model=(getattr(response, "response_metadata", None) or {}).get("model_name"),
        )
    except Exception as e:  # noqa: BLE001 - telemetry must never be fatal
        logger.debug("turn_metrics: raw model call not recorded: %s", e)


def _describe_if_bad_request(exc, messages, tools, label):
    """Log the redacted shape of a request a provider rejected (raw-node path)."""
    if messages is None:
        return
    try:
        from app.agents.leonardo.message_invariants import (
            log_bad_request_shape,
            looks_like_bad_request,
        )

        if looks_like_bad_request(exc):
            log_bad_request_shape(exc, messages, tools=tools, label=label)
    except Exception:  # noqa: BLE001 - diagnosis must never mask the real error
        logger.debug("could not describe the rejected request", exc_info=True)


def invoke_with_transient_retry(fn, *, label: str = "model call", messages=None, tools=None):
    """Call ``fn()`` with rung-1 transient-error retry semantics, returning its result.

    For raw StateGraph nodes (e.g. rails_beginner_agent, rails_ai_builder_agent)
    that invoke the model directly and thus never run through
    ``DynamicModelMiddleware.wrap_model_call`` — the only other place this ladder
    lives. Re-calls ``fn`` up to ``_MODEL_RETRY_MAX_ATTEMPTS`` times with capped
    exponential backoff while ``is_transient_error`` is true; a deterministic
    error (bad kwarg -> TypeError, 400) or a persistent transient one re-raises so
    it can flow on to the fallback/floor rungs instead of retrying identically.

    ``fn`` is a zero-arg thunk so the caller keeps full control of how the model is
    invoked (bind_tools, cache_control kwargs, message list, etc.).

    This is also where the raw agents contribute to per-turn performance
    telemetry: TurnMetricsMiddleware only wraps ``create_agent`` agents, so
    without timing here beginner mode would report zero model time and a wildly
    inflated overhead. Retries are folded into ONE recorded call carrying the
    total wait — that is what the user actually sat through. See
    app/lib/turn_metrics.py.
    """
    from app.lib.turn_metrics import current_turn

    turn = current_turn()
    started_at = time.monotonic()
    attempt = 0
    while True:
        try:
            result = fn()
        except Exception as e:
            attempt += 1
            # The wall clock gets a vote alongside the counter. Five attempts is
            # the right number when each costs a second and the wrong number when
            # each costs a chunk timeout — see _MODEL_RETRY_MAX_TOTAL_SECONDS.
            out_of_budget = retry_budget_spent(started_at)
            if (
                attempt >= _MODEL_RETRY_MAX_ATTEMPTS
                or out_of_budget
                or not is_transient_error(e)
            ):
                if out_of_budget and is_transient_error(e):
                    logger.warning(
                        "Giving up on %s after %.0fs of transient failures "
                        "(%d attempts): %r",
                        label, time.monotonic() - started_at, attempt, e,
                    )
                # Same diagnosis the middleware path records: a provider 400
                # carries nothing actionable, so describe the shape we sent.
                _describe_if_bad_request(e, messages, tools, label)
                _record_raw_model_call(turn, started_at, None)
                raise
            logger.warning(
                "Transient error on %s (attempt %d/%d): %r; retrying",
                label, attempt, _MODEL_RETRY_MAX_ATTEMPTS - 1, e,
            )
            _notify_slow_retry(label, attempt, time.monotonic() - started_at)
            time.sleep(_model_retry_delay(attempt))
        else:
            _record_raw_model_call(turn, started_at, result)
            return result
