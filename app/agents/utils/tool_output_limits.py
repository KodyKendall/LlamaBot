"""One enforcement point for the size of every tool result.

Why this exists
---------------
Third time is the pattern, not the accident. Three separate incidents produced
the same customer-visible symptom — Leo stops, no text, no tool calls, just a
spinner — because one unbounded string got into the message list:

- **SI#106** — the delta reducer dropped ``REMOVE_ALL_MESSAGES``, so compaction
  never actually shrank anything.
- **SI#246** — an unbounded inbound ``debug_info.full_html``. Fixed by
  ``app/websocket/payload_limits.py``, which caps what the *browser* sends.
- **2026-08-13** — an unbounded ``grep_files`` result: one match inside a
  minified vendor bundle returned a single 294,807-character ``ToolMessage``
  (~74k tokens). Nothing capped what the agent's own *tools* produce.

Each fix was correct and each time the loop came back through a different door,
because we capped the input we happened to be thinking about. The caps we had on
tool output were three ad-hoc ones (``read_file`` clamps lines, the bash and log
tools call ``truncate_output``) and one tool with none — which is exactly how
this got through.

Why the ceiling is what it is
-----------------------------
``SUMMARIZATION_KEEP_TOKENS`` (30k) is the preserved recent tail, and
``SummarizationMiddleware`` never splits a tool-call group. So a single tool
result larger than that tail **cannot be compacted away, ever** — it survives
every pass and pins the thread over the trigger for the rest of its life. That
makes the keep-tail budget the natural ceiling; the default here is a fraction of
it (~20k tokens / 80 KB) so a capped result still leaves room for the rest of the
turn.

Truncation is head-and-tail with an explicit notice rather than a drop, so the
agent can always tell partial output from broken output and knows to re-read the
source directly.

Wired in at both agent shapes, so there is genuinely one choke point:

- ``ToolResultSizeLimitMiddleware`` (``app/agents/leonardo/tool_output_middleware.py``)
  for every ``create_agent`` agent, added centrally in ``build_leonardo_agent``.
- ``cap_tool_node_output`` / ``CappedToolNode`` for the raw ``StateGraph`` agents,
  which never see middleware at all.
"""
from __future__ import annotations

import dataclasses
import logging
import os

from langchain_core.messages import ToolMessage
from langgraph.prebuilt import ToolNode

from app.lib.text_budget import byte_len

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_RESULT_MAX_BYTES",
    "TOOL_RESULT_TRUNCATION_NOTICE",
    "CappedToolNode",
    "cap_tool_message",
    "cap_tool_node_output",
    "cap_tool_result",
]


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


# Hard ceiling on a single tool result. ~80 KB is ~20k tokens: well under the
# 30k-token keep-tail, so a result this size is always compactable, and far more
# output than any tool call has ever usefully returned.
TOOL_RESULT_MAX_BYTES = _env_int("TOOL_RESULT_MAX_BYTES", 80 * 1024)

TOOL_RESULT_TRUNCATION_NOTICE = (
    "\n\n[TOOL OUTPUT TRUNCATED — this result was too large to keep in the "
    "conversation. The middle was dropped; the beginning and end are shown. Do "
    "not assume the omitted part was empty. If you need what was dropped, narrow "
    "the search or read the specific file directly rather than re-running the "
    "same command.]\n\n"
)


def cap_tool_result(content, *, tool_name=None, max_bytes: int | None = None):
    """Bound one tool result's text. Returns `content` itself when it fits."""
    if not isinstance(content, str):
        return content
    limit = max_bytes or TOOL_RESULT_MAX_BYTES
    original = byte_len(content)
    if original <= limit:
        return content

    # The notice is part of the payload, so reserve room for it up front and the
    # returned string is genuinely under the cap the caller was promised.
    body_budget = max(1024, limit - byte_len(TOOL_RESULT_TRUNCATION_NOTICE))
    head_budget = body_budget * 3 // 4
    tail_budget = body_budget - head_budget
    raw = content.encode("utf-8", "ignore")
    head = raw[:head_budget].decode("utf-8", "ignore")
    tail = raw[len(raw) - tail_budget:].decode("utf-8", "ignore")
    capped = head + TOOL_RESULT_TRUNCATION_NOTICE + tail

    logger.warning(
        "Capped oversized tool result from %s: %d bytes -> %d bytes (limit %d). "
        "A result larger than the summarization keep-tail can never be compacted "
        "away and would wedge this thread.",
        tool_name or "unknown tool", original, byte_len(capped), limit,
    )
    return capped


def _cap_content_blocks(blocks, *, tool_name, max_bytes):
    """Cap the text blocks of a multimodal result, leaving media alone.

    A truncated base64 image is a broken image, and screenshots are already
    handled by ``ToolResultImageClearingMiddleware``. Text is what runs away.
    """
    out = []
    changed = False
    for block in blocks:
        if isinstance(block, dict) and block.get("type") in ("text", "text_delta"):
            text = block.get("text")
            capped = cap_tool_result(text, tool_name=tool_name, max_bytes=max_bytes)
            if capped is not text:
                block = {**block, "text": capped}
                changed = True
        out.append(block)
    return out if changed else blocks


def cap_tool_message(message, *, max_bytes: int | None = None):
    """Return `message` bounded to the cap, or `message` itself if it fits.

    Only the content is rewritten: ``tool_call_id``, ``name`` and ``id`` are
    preserved, because a size fix that breaks an AI/Tool pair turns a slow thread
    into one that does not run at all.
    """
    if not isinstance(message, ToolMessage):
        return message

    content = message.content
    tool_name = getattr(message, "name", None)

    if isinstance(content, str):
        capped = cap_tool_result(content, tool_name=tool_name, max_bytes=max_bytes)
    elif isinstance(content, list):
        capped = _cap_content_blocks(content, tool_name=tool_name, max_bytes=max_bytes)
    else:
        return message

    if capped is content:
        return message
    return message.model_copy(update={"content": capped})


def cap_tool_node_output(output, *, max_bytes: int | None = None):
    """Cap every ToolMessage in whatever shape a ToolNode handed back.

    ``ToolNode`` returns a ``{"messages": [...]}`` dict for tools that return
    values, and a list of ``Command`` updates for tools that return ``Command``
    (which is what most rails_agent tools do). Both shapes — and lists mixing
    them — go through here. Anything unrecognized is passed through untouched:
    this must never be the thing that breaks a turn.
    """
    if isinstance(output, ToolMessage):
        return cap_tool_message(output, max_bytes=max_bytes)

    if isinstance(output, list):
        return [cap_tool_node_output(item, max_bytes=max_bytes) for item in output]

    if isinstance(output, dict):
        messages = output.get("messages")
        if isinstance(messages, list):
            return {
                **output,
                "messages": [cap_tool_message(m, max_bytes=max_bytes) for m in messages],
            }
        return output

    update = getattr(output, "update", None)
    if isinstance(update, dict) and isinstance(update.get("messages"), list):
        capped = [cap_tool_message(m, max_bytes=max_bytes) for m in update["messages"]]
        if all(a is b for a, b in zip(capped, update["messages"])):
            return output
        new_update = {**update, "messages": capped}
        # `Command` is a dataclass and rejects attribute assignment, so rebuild
        # it rather than mutating. Whatever else it carries (goto, graph, resume)
        # must survive or the graph takes a different edge than the tool asked for.
        try:
            return dataclasses.replace(output, update=new_update)
        except TypeError:
            pass
        try:
            output.update = new_update
            return output
        except (AttributeError, TypeError):
            logger.warning(
                "Could not cap an oversized tool result carried by %s",
                type(output).__name__,
            )
        return output

    return output


class CappedToolNode(ToolNode):
    """A ``ToolNode`` whose results are bounded, for the raw StateGraph agents.

    The ``create_agent`` agents get this cap from
    ``ToolResultSizeLimitMiddleware``, but several agents build their graph by
    hand and never run middleware at all — so the guarantee has to be re-made
    here or it isn't a guarantee. Drop-in: ``CappedToolNode(tools)`` wherever
    ``ToolNode(tools)`` was.

    Subclassing (rather than wrapping) keeps it a real ``Runnable``, which is
    what ``builder.add_node`` requires.
    """

    def invoke(self, *args, **kwargs):
        return cap_tool_node_output(super().invoke(*args, **kwargs))

    async def ainvoke(self, *args, **kwargs):
        return cap_tool_node_output(await super().ainvoke(*args, **kwargs))
