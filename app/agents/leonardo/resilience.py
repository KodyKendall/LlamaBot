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
