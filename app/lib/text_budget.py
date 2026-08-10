"""Byte- and token-budgeted text truncation.

Shared by the two places that have to say "no" to unbounded content:

- ``app.websocket.payload_limits`` — inbound WebSocket frames, so a 10 MB
  rendered page never becomes LangGraph state (SupportIncident #246), and
- ``app.agents.leonardo.summarization`` — the messages compaction preserves
  verbatim, so one fat early message can't pin a thread over the summarization
  trigger forever.

Every truncation leaves the same machine- and human-readable marker,
``[truncated: N bytes omitted]``, so the agent always knows it is looking at a
partial document rather than silently receiving broken HTML.
"""
from __future__ import annotations

import re
from typing import Callable

# Exact wording is part of the contract: tests match it and the agent reads it.
TRUNCATION_MARKER_RE = re.compile(r"\[truncated: (\d+) bytes omitted\]")


def truncation_marker(omitted_bytes: int) -> str:
    return f"\n... [truncated: {omitted_bytes} bytes omitted] ...\n"


def byte_len(text: str) -> int:
    return len(text.encode("utf-8", "ignore"))


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "ignore")


def truncate_text(text: str, max_bytes: int, *, mode: str = "middle") -> str:
    """Clamp `text` to `max_bytes` INCLUDING the marker.

    ``mode="middle"`` keeps the head and the tail (the head carries the user's
    intent, the tail the closing markup); ``mode="head"`` keeps only the head.
    Callers can trust the returned size, so a cap is never blown by the marker
    that announces the cap.
    """
    if not isinstance(text, str):
        return text
    raw = text.encode("utf-8", "ignore")
    if len(raw) <= max_bytes:
        return text

    marker = truncation_marker(len(raw) - max_bytes)
    budget = max(0, max_bytes - byte_len(marker))

    if mode == "head":
        head_n, tail_n = budget, 0
    else:
        head_n = budget * 3 // 4
        tail_n = budget - head_n

    head = _decode(raw[:head_n]) if head_n else ""
    tail = _decode(raw[len(raw) - tail_n:]) if tail_n else ""
    marker = truncation_marker(len(raw) - head_n - tail_n)
    result = head + marker + tail

    # The recomputed marker can be a digit longer than the estimate; shave the
    # head rather than hand back something over the cap the caller was promised.
    while byte_len(result) > max_bytes and head:
        head = _decode(head.encode("utf-8", "ignore")[: max(0, byte_len(head) - 16)])
        result = head + marker + tail
    return result


def shrink_to_token_budget(
    text: str,
    max_tokens: int,
    count_tokens: Callable[[str], int],
    *,
    mode: str = "middle",
    min_bytes: int = 512,
) -> str:
    """Truncate `text` until `count_tokens(text) <= max_tokens`.

    Tokens per byte vary with the content (HTML tokenizes very differently from
    prose), so this converges by measuring rather than by assuming a ratio.
    """
    if not isinstance(text, str) or count_tokens(text) <= max_tokens:
        return text

    target = max(min_bytes, max_tokens * 4)
    for _ in range(12):
        out = truncate_text(text, target, mode=mode)
        if count_tokens(out) <= max_tokens:
            return out
        if target <= min_bytes:
            break
        target = max(min_bytes, target * 2 // 3)
    return truncate_text(text, min_bytes, mode=mode)
