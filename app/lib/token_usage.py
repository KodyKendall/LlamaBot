"""Extract token-usage metadata (including prompt-cache hits) from LLM responses.

LangChain standardizes per-call usage into ``AIMessage.usage_metadata``, shaped like::

    {
        "input_tokens": int,         # total prompt tokens (includes cached + cache-write)
        "output_tokens": int,
        "total_tokens": int,
        "input_token_details": {"cache_read": int, "cache_creation": int, ...},
        "output_token_details": {"reasoning": int, ...},
    }

The ``input_token_details`` sub-dict is where prompt-cache info lives. It is populated for
both DeepSeek (the default model — via the OpenAI-compatible ``prompt_tokens_details.cached_tokens``,
which LangChain maps to ``cache_read``) and Anthropic/Claude (``cache_read`` + ``cache_creation``).

Forwarding only the three top-level scalars drops the cache signal, so we surface the cache
fields here too. The mothership computes cache-hit rate as
``cache_read_input_tokens / input_tokens``.
"""
from typing import Any, Optional


def _get(usage: Any, key: str, default: int = 0) -> Any:
    """Read ``key`` off a usage_metadata value that may be a dict or an object."""
    if isinstance(usage, dict):
        return usage.get(key, default)
    return getattr(usage, key, default)


def extract_token_usage(message: Any) -> Optional[dict]:
    """Return a token-usage dict for an LLM response message, or ``None`` if unavailable.

    Includes prompt-cache fields (``cache_read_input_tokens`` / ``cache_creation_input_tokens``)
    so downstream consumers can measure cache-hit rate.
    """
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return None

    details = _get(usage, "input_token_details", {}) or {}

    return {
        "input_tokens": _get(usage, "input_tokens"),
        "output_tokens": _get(usage, "output_tokens"),
        "total_tokens": _get(usage, "total_tokens"),
        "cache_read_input_tokens": _get(details, "cache_read"),
        "cache_creation_input_tokens": _get(details, "cache_creation"),
    }
