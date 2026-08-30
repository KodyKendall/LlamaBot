"""One validator every provider call passes through.

2026-08-23 fleet telemetry, P1-4. Three different malformed-history shapes were
still killing turns on customer boxes in 30 days:

  | shape                                                    | boxes | path |
  |----------------------------------------------------------|-------|------|
  | ``messages`` must contain at least one message with role  |   4   | the question-card resume |
  | ``user`` or ``tool``                                       |       | |
  | ``messages[33].content`` did not match any supported type |   1   | rails_beginner_agent |
  | assistant ``tool_calls`` not followed by ``tool`` messages|   2   | 0.6.0x boxes |

Only the third had a fix, and it had TWO implementations —
``repair_orphaned_tool_calls_in_messages`` called by hand inside the raw
``StateGraph`` nodes ("this raw node never runs AgentMiddleware"), and
``RepairOrphanedToolCallsMiddleware`` for the factory-built modes. Two
implementations of one rule means every new path starts out missing it, which is
exactly how the question-resume path ended up unprotected.

So: one function, :func:`normalize_messages_for_provider`, asserting the
invariants the providers actually enforce, called at every model-call boundary —
the middleware for ``create_agent`` modes, and directly by the raw nodes.

It never raises and never drops content it cannot prove is broken. Repairing a
history is a last resort before a hard 400; being conservative costs a slightly
odd message, being wrong costs the turn.
"""
import json
import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.leonardo.agent_factory import (
    emitted_tool_calls,
    repair_orphaned_tool_calls_in_messages,
)

logger = logging.getLogger(__name__)

#: Block types every provider we ship against accepts inside a content list.
SUPPORTED_BLOCK_TYPES = frozenset({
    "text", "image", "image_url", "input_image", "video", "video_url",
    "file", "document", "input_file", "thinking", "redacted_thinking",
    "reasoning", "tool_use", "tool_result", "cache_control",
})

#: What an assistant turn with no text becomes. An empty string is rejected
#: outright by several providers; a space is not, and says nothing.
_EMPTY_AI_PLACEHOLDER = "(no content)"

_NO_USER_MESSAGE = (
    "<NOTE_FROM_SYSTEM>Continue from the conversation above.</NOTE_FROM_SYSTEM>"
)


def _is_role(msg, role: str) -> bool:
    if isinstance(msg, dict):
        return msg.get("role") == role
    return getattr(msg, "type", None) == role


def _content_of(msg):
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _with_content(msg, content):
    if isinstance(msg, dict):
        out = dict(msg)
        out["content"] = content
        return out
    try:
        return msg.model_copy(update={"content": content})
    except Exception:  # noqa: BLE001 - never let a repair break the turn
        return msg


def _normalize_block(block):
    """Return a block the provider will accept, or None to drop it."""
    if isinstance(block, str):
        return {"type": "text", "text": block} if block.strip() else None
    if not isinstance(block, dict):
        # A bare object in a content list is the `content did not match any
        # supported type` 400. Stringify rather than drop: it may be data the
        # agent needs.
        try:
            return {"type": "text", "text": json.dumps(block, default=str)}
        except Exception:  # noqa: BLE001
            return {"type": "text", "text": str(block)}

    block_type = block.get("type")
    if block_type in SUPPORTED_BLOCK_TYPES:
        return block
    if block_type is None:
        # Untyped dict — treat it as text if it plausibly is, else stringify.
        if "text" in block:
            return {"type": "text", "text": str(block["text"])}
        return {"type": "text", "text": json.dumps(block, default=str)}

    logger.warning(
        "message_invariants: stringifying unsupported content block type %r",
        block_type,
    )
    return {"type": "text", "text": json.dumps(block, default=str)}


def _normalize_content(msg):
    """Coerce one message's content into something a provider accepts.

    Returns the message unchanged when it already is.
    """
    content = _content_of(msg)

    if isinstance(content, list):
        blocks = [b for b in (_normalize_block(b) for b in content) if b is not None]
        if not blocks:
            # A content list that normalized down to nothing is the empty-content
            # 400 in a different costume.
            if _is_role(msg, "ai") and emitted_tool_calls(msg):
                return _with_content(msg, "")  # tool-call-only turns may be empty
            return _with_content(msg, _EMPTY_AI_PLACEHOLDER)
        if blocks == list(content):
            return msg
        return _with_content(msg, blocks)

    if content is None:
        return _with_content(msg, "" if _is_role(msg, "ai") else _EMPTY_AI_PLACEHOLDER)

    if isinstance(content, str):
        if content.strip():
            return msg
        # Empty string. Legal on an assistant turn that is only tool calls;
        # rejected by several providers everywhere else.
        if _is_role(msg, "ai") and emitted_tool_calls(msg):
            return msg
        if _is_role(msg, "tool"):
            return _with_content(msg, "(the tool returned nothing)")
        return _with_content(msg, _EMPTY_AI_PLACEHOLDER)

    return _with_content(msg, str(content))


def _has_user_or_tool(messages) -> bool:
    return any(_is_role(m, "human") or _is_role(m, "tool") for m in messages)


def normalize_messages_for_provider(messages: List[Any]) -> List[Any]:
    """Enforce every invariant a provider actually checks, in one place.

    1. Every announced ``tool_calls`` id is answered by a ``tool`` message, and
       every ``tool`` message answers something (delegated to
       :func:`repair_orphaned_tool_calls_in_messages`, which owns that rule).
    2. Every ``content`` is a supported type.
    3. No message carries empty content where the provider forbids it.
    4. At least one ``user``/``tool`` message is present.

    Idempotent, and returns the SAME list object when nothing needed changing so
    callers can cheaply detect "no change".
    """
    # NOT ``list(messages)``: repair_orphaned_tool_calls_in_messages returns the
    # SAME list object when it changes nothing, and a defensive copy here would
    # throw that signal away — every call would then look "changed" and no caller
    # could detect a no-op. It does not mutate what it is given.
    try:
        repaired = repair_orphaned_tool_calls_in_messages(messages)
    except Exception:  # noqa: BLE001
        logger.exception("message_invariants: tool-call repair failed; skipping it")
        repaired = messages

    changed = repaired is not messages
    out = []
    for msg in repaired:
        fixed = _normalize_content(msg)
        changed = changed or (fixed is not msg)
        out.append(fixed)

    # A history with nothing but system/assistant messages is rejected outright:
    # "`messages` must contain at least one message with role `user` or `tool`".
    # Seen on the question-card resume path across 4 boxes.
    if out and not _has_user_or_tool(out):
        logger.warning(
            "message_invariants: no user/tool message in a %d-message history; "
            "appending a continuation note so the provider accepts it",
            len(out),
        )
        out.append(HumanMessage(content=_NO_USER_MESSAGE))
        changed = True

    return out if changed else messages


# ---------------------------------------------------------------------------
# Diagnosis — what did we actually send?
# ---------------------------------------------------------------------------
#
# P1-3: our DEFAULT model (muse-spark-1.2-contributor) returns a bare
# `400 invalid_request_error` with `'param': None` — 32 occurrences on 7 boxes in
# 7 days, the largest live LlamaBot error class, and nothing in the payload to act
# on from the mothership. So when a provider rejects a request, describe the SHAPE
# of what we sent. Content is deliberately never included: we need the shape, not
# the text.

def describe_message_shape(messages, *, tools=None, model=None) -> Dict[str, Any]:
    """A redacted, structural description of a model request, for error reports."""
    per_message = []
    total_chars = 0
    for index, msg in enumerate(messages or []):
        content = _content_of(msg)
        if isinstance(content, list):
            content_kind = "list"
            parts = [
                (b.get("type") if isinstance(b, dict) else type(b).__name__)
                for b in content
            ]
            chars = sum(len(str(b)) for b in content)
        elif isinstance(content, str):
            content_kind = "str"
            parts = []
            chars = len(content)
        else:
            content_kind = type(content).__name__
            parts = []
            chars = len(str(content or ""))
        total_chars += chars

        calls = emitted_tool_calls(msg) if _is_role(msg, "ai") else []
        entry = {
            "i": index,
            "role": (msg.get("role") if isinstance(msg, dict)
                     else getattr(msg, "type", type(msg).__name__)),
            "content": content_kind,
            "chars": chars,
            "empty": chars == 0,
        }
        if parts:
            entry["blocks"] = parts
        if calls:
            entry["tool_calls"] = [c.get("name") for c in calls]
            entry["tool_call_ids"] = [c.get("id") for c in calls]
        tool_call_id = getattr(msg, "tool_call_id", None) or (
            msg.get("tool_call_id") if isinstance(msg, dict) else None
        )
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        per_message.append(entry)

    announced = {
        cid
        for m in (messages or []) if _is_role(m, "ai")
        for cid in (c.get("id") for c in emitted_tool_calls(m)) if cid
    }
    answered = {
        (getattr(m, "tool_call_id", None) or (m.get("tool_call_id") if isinstance(m, dict) else None))
        for m in (messages or []) if _is_role(m, "tool")
    }
    answered.discard(None)

    return {
        "model": model,
        "message_count": len(messages or []),
        "total_content_chars": total_chars,
        "approx_tokens": total_chars // 4,
        "tool_count": len(tools or []),
        "has_user_or_tool": _has_user_or_tool(messages or []),
        "unanswered_tool_call_ids": sorted(announced - answered),
        "unanchored_tool_message_ids": sorted(answered - announced),
        "messages": per_message,
    }


def looks_like_bad_request(exc) -> bool:
    """True for a provider rejecting the request itself (a 400), not a transport blip."""
    if exc.__class__.__name__ in ("BadRequestError", "InvalidRequestError",
                                  "UnprocessableEntityError"):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return status in (400, 422)


# The three distinct conditions hiding behind BadRequestError on the fleet. Counting them
# as one made P1-3 look bigger than it is and hid the fact that two of them already have
# owners: the context ceiling is not a bad request, and malformed history is the repair
# path's class. Substrings, because providers reword their prose but keep the shape.
_BAD_REQUEST_SIGNATURES = (
    ("context_length_exceeded", ("maximum context length", "context_length_exceeded",
                                 "reduce the length of the messages")),
    ("malformed_history", ("must contain at least one message",
                           "insufficient tool messages",
                           "tool_call_id", "invalid_request_error: messages")),
    ("invalid_parameters", ("contains invalid parameters", "invalid parameters",
                            "check the request body")),
)


def classify_bad_request(exc) -> str:
    """Name which KIND of 400 this is, so three different bugs stop being one number.

    Returns "unknown" rather than guessing: an unrecognised rejection is a thing we want
    to see arrive, not something to file under the nearest existing label.
    """
    text = str(getattr(exc, "message", "") or exc or "").lower()
    for name, needles in _BAD_REQUEST_SIGNATURES:
        if any(needle in text for needle in needles):
            return name
    return "unknown"


# Anything whose NAME says credential. The payload reaches the mothership, so a key in it
# would be a disclosure rather than a clue.
_SECRET_KEY_HINTS = ("key", "token", "secret", "password", "authorization", "credential")

# ...except these, which merely CONTAIN one of those words. `max_tokens` is the parameter
# most likely to be the culprit in an "invalid parameters" rejection, so redacting it would
# have removed the single most useful field from the payload this exists to produce.
_NEVER_SECRET = frozenset({
    "max_tokens", "max_completion_tokens", "max_output_tokens", "max_new_tokens",
    "max_prompt_tokens", "token_usage", "logit_bias", "top_logprobs",
})

# Message bodies are described separately by describe_message_shape(); copying them here
# would duplicate user content into a second payload for no diagnostic gain.
_BULK_KEYS = ("messages", "tools", "input", "prompt", "contents")

_MAX_PARAM_CHARS = 512


def redact_request_params(params: Dict[str, Any] | None) -> Dict[str, Any]:
    """The outgoing request parameters, safe to log.

    P1-3 is a provider answering "the request contains invalid parameters" and then
    declining to say WHICH one — `param` is null on every one of the 15 occurrences. We
    have never recorded what we sent, so there is nothing to compare a working box
    against. Unknown keys are deliberately kept: the next offending parameter is, by
    definition, one we are not thinking about today.
    """
    out: Dict[str, Any] = {}
    for key, value in (params or {}).items():
        lowered = str(key).lower()
        if lowered in _BULK_KEYS:
            continue
        if lowered not in _NEVER_SECRET and any(hint in lowered for hint in _SECRET_HINT_SET):
            out[key] = "[redacted]"
            continue
        if isinstance(value, dict):
            out[key] = redact_request_params(value)
            continue
        text = str(value)
        out[key] = value if len(text) <= _MAX_PARAM_CHARS else text[:_MAX_PARAM_CHARS]
    return out


_SECRET_HINT_SET = _SECRET_KEY_HINTS


def log_bad_request_shape(exc, messages, *, tools=None, model=None, label: str = "",
                          request_params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Log (and return) the shape of a request a provider rejected.

    Returned so the caller can attach it to a ``report_error`` payload; the
    mothership has nothing else to go on when the provider says ``'param': None``.
    """
    shape = describe_message_shape(messages, tools=tools, model=model)
    # WHICH 400 this is, and what we actually sent. Without these the mothership sees a
    # message it cannot act on and three unrelated bugs averaged into one count.
    shape["bad_request_kind"] = classify_bad_request(exc)
    if request_params:
        shape["request_params"] = redact_request_params(request_params)
    try:
        logger.error(
            "Provider rejected the request (%s) %s — request shape: %s",
            exc.__class__.__name__, label, json.dumps(shape, default=str)[:8000],
        )
    except Exception:  # noqa: BLE001
        logger.error("Provider rejected the request (%s) %s", exc.__class__.__name__, label)
    try:
        exc.llamabot_request_shape = shape
    except Exception:  # noqa: BLE001 - some exception types forbid attributes
        pass
    return shape
