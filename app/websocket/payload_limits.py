"""Bounded ingestion of inbound WebSocket frames (SupportIncident #246).

`RequestHandler.get_langgraph_app_and_state` used to copy every non-routing frame
field into LangGraph state verbatim — "pass everything else through naturally".
On a customer box that meant a `debug_info.full_html` of **10.6 MB** (the whole
rendered `/conversations` page, every meeting transcript inlined) landing in
state on EVERY turn, and therefore in every checkpoint: 20 MB of checkpoint blobs
for a three-message thread. The agent never reads that HTML — only `view_path`
and `request_path` — and summarization structurally cannot reclaim it, so the
thread wedged in a compaction loop and the customer had to abandon it.

The rule here is deliberately about the **class**, not those two fields: nothing a
client sends becomes an unbounded state value, whatever the key. The frontend
caps the same payloads (see the Leonardo `element_selector.js` / page-context
partial), but client-side caps can be bypassed and old boxes run old frontends —
this backstop ships in the image and applies to every frame.

Everything is truncated with an explicit `[truncated: N bytes omitted]` marker
rather than dropped, so `view_path`/`request_path` always survive and the agent
can tell a partial document from a broken one.
"""
from __future__ import annotations

import json
import logging
import os
import re

from app.lib.text_budget import (
    TRUNCATION_MARKER_RE,
    byte_len,
    truncate_text,
    truncation_marker,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEBUG_INFO_VALUE_MAX_BYTES",
    "MESSAGE_TEXT_MAX_BYTES",
    "SELECTED_ELEMENT_MAX_BYTES",
    "STATE_VALUE_MAX_BYTES",
    "TRUNCATION_MARKER_RE",
    "cap_debug_info",
    "cap_message_text",
    "cap_state_value",
    "serialized_size",
]


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


# Per-leaf cap inside a `debug_info` dict. 32 KB is far more page context than any
# agent has ever read from it, and ~300x smaller than what the incident box sent.
DEBUG_INFO_VALUE_MAX_BYTES = _env_int("DEBUG_INFO_VALUE_MAX_BYTES", 32 * 1024)

# Hard ceiling on the serialized size of ANY single non-routing state value.
STATE_VALUE_MAX_BYTES = _env_int("STATE_VALUE_MAX_BYTES", 128 * 1024)

# Cap on the user's message text. Chat messages are prose; the only thing that
# ever gets near this is markup pasted in by the element picker.
MESSAGE_TEXT_MAX_BYTES = _env_int("MESSAGE_TEXT_MAX_BYTES", 64 * 1024)

# Tighter cap for a `<SELECTED_ELEMENT>` block specifically, so a picked element
# can never crowd out the sentence the user wrote about it.
SELECTED_ELEMENT_MAX_BYTES = _env_int("SELECTED_ELEMENT_MAX_BYTES", 24 * 1024)

_SELECTED_ELEMENT_RE = re.compile(
    r"(<SELECTED_ELEMENT>)(.*?)(</SELECTED_ELEMENT>)", re.DOTALL
)


def serialized_size(value) -> int:
    """Byte size of `value` as it would be stored in a checkpoint."""
    if isinstance(value, str):
        return byte_len(value)
    try:
        return byte_len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return byte_len(str(value))


# ---------------------------------------------------------------------------
# Message text
# ---------------------------------------------------------------------------

def cap_message_text(text):
    """Bound the user's message body, truncating picked elements first.

    The element picker inlines a whole `outerHTML` into the message text, which —
    unlike `debug_info` — really does reach the model and really is counted by
    the summarization trigger. A 375 KB `<section>` is ~95k tokens in a single
    HumanMessage, and it is always in the preserved recent tail, so no amount of
    compaction can get back under the threshold.
    """
    if not isinstance(text, str) or byte_len(text) <= MESSAGE_TEXT_MAX_BYTES:
        return text

    original = byte_len(text)

    def _cap_block(match: re.Match) -> str:
        open_tag, body, close_tag = match.group(1), match.group(2), match.group(3)
        if byte_len(body) <= SELECTED_ELEMENT_MAX_BYTES:
            return match.group(0)
        return open_tag + truncate_text(body, SELECTED_ELEMENT_MAX_BYTES) + close_tag

    capped = _SELECTED_ELEMENT_RE.sub(_cap_block, text)

    # An unterminated <SELECTED_ELEMENT>, or plain oversized text, falls through
    # to the whole-message cap.
    if byte_len(capped) > MESSAGE_TEXT_MAX_BYTES:
        capped = truncate_text(capped, MESSAGE_TEXT_MAX_BYTES)

    logger.warning(
        "Capped oversized message text: %d bytes -> %d bytes (limit %d)",
        original, byte_len(capped), MESSAGE_TEXT_MAX_BYTES,
    )
    return capped


# ---------------------------------------------------------------------------
# State values
# ---------------------------------------------------------------------------

# A member smaller than this isn't worth keeping a truncated stub of.
_MIN_MEMBER_BYTES = 1024


def _fit_serialized(text: str, max_bytes: int) -> str:
    """Truncate `text` until its SERIALIZED size fits.

    JSON-escaping inflates markup (every `"` becomes `\\"`, every newline `\\n`),
    so truncating to a raw byte count doesn't reliably land under a checkpoint
    budget. Converge by measuring instead of assuming a ratio.
    """
    for _ in range(6):
        size = serialized_size(text)
        if size <= max_bytes:
            return text
        raw = byte_len(text)
        target = max(64, int(raw * max_bytes / size) - 64)
        text = truncate_text(text, target)
    return text


def _cap_leaf(value, max_bytes: int, path: str):
    """Recursively bound every string leaf under `value`."""
    if isinstance(value, str):
        if serialized_size(value) <= max_bytes:
            return value
        capped = _fit_serialized(value, max_bytes)
        logger.warning(
            "Capped oversized inbound field %s: %d bytes -> %d bytes (limit %d)",
            path, byte_len(value), byte_len(capped), max_bytes,
        )
        return capped
    if isinstance(value, dict):
        return {k: _cap_leaf(v, max_bytes, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cap_leaf(v, max_bytes, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Anything else (a custom object) is only ever stored as its repr.
    return _cap_leaf(str(value), max_bytes, path)


def _enforce_total(value, max_bytes: int, path: str):
    """Bound a value whose members are each legal but together are not.

    Members are shrunk to a fair share rather than the container being dropped:
    `debug_info` must keep answering `.get("view_path")`, so the shape is always
    preserved even when the content is not.
    """
    for _ in range(6):
        if serialized_size(value) <= max_bytes:
            return value

        if isinstance(value, str):
            value = _fit_serialized(value, max_bytes)
            continue

        if isinstance(value, dict):
            keys = list(value)
        elif isinstance(value, (list, tuple)):
            keys = list(range(len(value)))
            value = list(value)
        else:
            return _fit_serialized(str(value), max_bytes)

        if not keys:
            return value

        share = (max_bytes * 9 // 10) // len(keys)
        out = dict(value) if isinstance(value, dict) else list(value)
        for key in keys:
            member_size = serialized_size(out[key])
            if member_size <= share:
                continue
            if share < _MIN_MEMBER_BYTES:
                out[key] = truncation_marker(member_size).strip()
                logger.warning(
                    "Dropped oversized inbound member %s[%s] (%d bytes) to keep %s "
                    "under %d bytes",
                    path, key, member_size, path, max_bytes,
                )
            else:
                out[key] = _enforce_total(out[key], share, f"{path}[{key}]")
                logger.warning(
                    "Shrank oversized inbound member %s[%s]: %d bytes -> %d bytes",
                    path, key, member_size, serialized_size(out[key]),
                )
        value = out
    return value


def cap_state_value(key: str, value, *, leaf_max_bytes: int | None = None):
    """Bound one non-routing frame field before it becomes LangGraph state."""
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if leaf_max_bytes is None:
        leaf_max_bytes = (
            DEBUG_INFO_VALUE_MAX_BYTES if key == "debug_info" else STATE_VALUE_MAX_BYTES
        )
    capped = _cap_leaf(value, leaf_max_bytes, key)
    return _enforce_total(capped, STATE_VALUE_MAX_BYTES, key)


def cap_debug_info(debug_info):
    """Bound the Rails page-debug payload.

    Kept as its own function because `debug_info` is the field with a known
    10 MB failure mode and a much tighter budget than everything else. The agents
    read exactly two keys out of it (`view_path`, `request_path`); those are tiny
    and always survive, while `full_html` is truncated rather than dropped so the
    legacy `view_page` tool still returns something usable.
    """
    if not isinstance(debug_info, dict):
        return debug_info
    return cap_state_value("debug_info", debug_info)
