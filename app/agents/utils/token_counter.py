"""Universal multimodal token counter for SummarizationMiddleware.

Uses tiktoken (cl100k_base) for text — accurate for DeepSeek, Claude, GPT-4, and a
close approximation for Gemini. Falls back to Google's countTokens API only when
multimodal blocks (images, PDFs, audio, video) are present.

Critically, this counter includes DeepSeek's `reasoning_content` (stored in
`additional_kwargs`), which the previous Gemini-only counter silently ignored —
causing compaction to never trigger for DeepSeek conversations.
"""

# Summarization threshold configuration
# This value is used by both backend (SummarizationMiddleware) and frontend (TokenIndicator)
SUMMARIZATION_TOKEN_THRESHOLD = 100000

import tiktoken
from google import genai
from google.genai import types
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

import logging
logger = logging.getLogger(__name__)

# Lazy-initialized Gemini client (only created if images are encountered)
_genai_client = None

# tiktoken encoder is initialized lazily on first use
_tiktoken_encoder = None

# Fixed estimate for a single image/media block (rough average across providers)
_IMAGE_TOKEN_ESTIMATE = 1000


def _get_tiktoken_encoder():
    global _tiktoken_encoder
    if _tiktoken_encoder is None:
        _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
    return _tiktoken_encoder


def _get_genai_client():
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client()
    return _genai_client


def _count_text(text: str) -> int:
    if not text:
        return 0
    try:
        return len(_get_tiktoken_encoder().encode(text, disallowed_special=()))
    except Exception:
        return len(text) // 4


def gemini_multimodal_token_counter(messages) -> int:
    """Universal token counter for SummarizationMiddleware.

    Strategy:
      - Text: tiktoken cl100k_base (within ~10% of DeepSeek/Claude/GPT actual; close
        enough for Gemini for threshold-triggering purposes).
      - DeepSeek reasoning_content: pulled from additional_kwargs and counted.
      - Tool calls / tool messages: name + serialized args/output counted as text.
      - Images / PDFs / video / audio: counted via Gemini countTokens API per-block
        for accuracy. Fixed estimate fallback if API fails.
    """
    total = 0
    has_multimodal = False
    multimodal_parts: list[types.Part] = []

    for msg in messages:
        total += _count_message_text(msg)

        # Collect multimodal parts for accurate counting via Gemini API
        mm_parts = _extract_multimodal_parts(msg)
        if mm_parts:
            has_multimodal = True
            multimodal_parts.extend(mm_parts)

        total += 3  # per-message overhead (role markers, etc.)

    if has_multimodal:
        total += _count_multimodal(multimodal_parts)

    return total


def _count_message_text(msg) -> int:
    """Count text tokens in a single message, including DeepSeek reasoning_content
    and tool call args / tool message output."""
    count = 0
    content = getattr(msg, "content", None)

    if isinstance(content, str):
        count += _count_text(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type in ("text", "text_delta"):
                    count += _count_text(block.get("text", ""))
                elif block_type == "thinking":
                    # Claude thinking blocks
                    count += _count_text(block.get("thinking", ""))
                elif block_type == "reasoning":
                    # OpenAI/GPT-5 reasoning blocks
                    count += _count_text(block.get("text", ""))
                    summary = block.get("summary")
                    if isinstance(summary, list):
                        for s in summary:
                            if isinstance(s, dict):
                                count += _count_text(s.get("text", ""))
                # image/media handled separately in _extract_multimodal_parts
            elif isinstance(block, str):
                count += _count_text(block)

    # DeepSeek stores reasoning_content in additional_kwargs (NOT in content).
    # This is the main reason DeepSeek compaction was never triggering.
    additional_kwargs = getattr(msg, "additional_kwargs", None) or {}
    reasoning = additional_kwargs.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        count += _count_text(reasoning)

    # Tool calls: count the function name and serialized args
    tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in tool_calls:
        if isinstance(tc, dict):
            count += _count_text(str(tc.get("name", "")))
            args = tc.get("args")
            if args is not None:
                count += _count_text(str(args))

    # ToolMessage output (content covered above, but artifact may exist)
    if isinstance(msg, ToolMessage):
        artifact = getattr(msg, "artifact", None)
        if artifact is not None:
            count += _count_text(str(artifact))

    return count


def _extract_multimodal_parts(msg) -> list[types.Part]:
    """Extract only image/media parts from a message for Gemini-based counting."""
    parts: list[types.Part] = []
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return parts

    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")
        if block_type == "image_url":
            url = block.get("image_url", {})
            if isinstance(url, dict):
                url = url.get("url", "")
            if isinstance(url, str) and url.startswith("data:"):
                try:
                    mime_end = url.index(";")
                    mime_type = url[5:mime_end]
                    b64_data = url.split(",", 1)[1]
                    parts.append(types.Part(
                        inline_data=types.Blob(mime_type=mime_type, data=b64_data),
                    ))
                except (ValueError, IndexError):
                    parts.append(types.Part(text="[image]"))
            elif isinstance(url, str) and url.startswith("http"):
                parts.append(types.Part(text="[image]"))

        elif block_type == "media":
            if "file_uri" in block:
                parts.append(types.Part(
                    file_data=types.FileData(
                        file_uri=block["file_uri"],
                        mime_type=block.get("mime_type", ""),
                    )
                ))
            elif "data" in block:
                parts.append(types.Part(
                    inline_data=types.Blob(
                        mime_type=block.get("mime_type", "application/octet-stream"),
                        data=block["data"],
                    )
                ))

    return parts


def _count_multimodal(parts: list[types.Part]) -> int:
    """Count multimodal parts via Gemini countTokens. Falls back to fixed estimate."""
    if not parts:
        return 0
    try:
        contents = [types.Content(role="user", parts=parts)]
        response = _get_genai_client().models.count_tokens(
            model="gemini-2.0-flash",
            contents=contents,
        )
        return response.total_tokens
    except Exception as e:
        logger.warning(f"Gemini countTokens failed for multimodal blocks, using estimate: {e}")
        return len(parts) * _IMAGE_TOKEN_ESTIMATE
