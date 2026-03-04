"""Gemini multimodal token counter for SummarizationMiddleware.

Uses Google's countTokens API for accurate multimodal token counting.
LangChain's default counter massively overcounts base64 images.
"""

# Summarization threshold configuration
# This value is used by both backend (SummarizationMiddleware) and frontend (TokenIndicator)
SUMMARIZATION_TOKEN_THRESHOLD = 100000

from google import genai
from google.genai import types
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

import logging
logger = logging.getLogger(__name__)

# Initialize Gemini client at module level
_genai_client = genai.Client()


def gemini_multimodal_token_counter(messages) -> int:
    """Accurate token counter using Google's countTokens API.

    Handles text, base64 images, PDFs, video, and audio correctly.
    Falls back to LangChain's default heuristic if API call fails.

    This counter is used by SummarizationMiddleware to determine when to trigger.
    For Gemini models, we get accurate counts. For other models (Claude, GPT, DeepSeek),
    we fall back to the standard LangChain approximation.
    """
    try:
        contents = _convert_langchain_to_genai_contents(messages)
        response = _genai_client.models.count_tokens(
            model="gemini-2.0-flash",
            contents=contents,
        )
        return response.total_tokens
    except Exception as e:
        logger.warning(f"countTokens API failed, falling back to heuristic: {e}")
        return _fallback_count(messages)


def _convert_langchain_to_genai_contents(messages) -> list[types.Content]:
    """Convert LangChain messages to Google GenAI Content format."""
    contents = []
    for msg in messages:
        role = _get_role(msg)
        parts = _extract_parts(msg)
        if parts:
            contents.append(types.Content(role=role, parts=parts))
    return contents


def _get_role(msg) -> str:
    if isinstance(msg, (HumanMessage, ToolMessage)):
        return "user"
    elif isinstance(msg, AIMessage):
        return "model"
    elif isinstance(msg, SystemMessage):
        return "user"  # Gemini countTokens doesn't have a system role
    return "user"


def _extract_parts(msg) -> list[types.Part]:
    """Extract parts from a LangChain message, handling multimodal content."""
    parts = []
    content = msg.content

    if isinstance(content, str):
        if content:
            parts.append(types.Part(text=content))
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(types.Part(text=text))

                elif block.get("type") == "image_url":
                    url = block.get("image_url", {})
                    if isinstance(url, dict):
                        url = url.get("url", "")
                    if isinstance(url, str) and url.startswith("data:"):
                        mime_end = url.index(";")
                        mime_type = url[5:mime_end]
                        b64_data = url.split(",", 1)[1]
                        parts.append(types.Part(
                            inline_data=types.Blob(
                                mime_type=mime_type,
                                data=b64_data,
                            )
                        ))
                    elif isinstance(url, str) and url.startswith("http"):
                        parts.append(types.Part(text="[image]"))

                elif block.get("type") == "media":
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
            elif isinstance(block, str):
                parts.append(types.Part(text=block))

    return parts


def _fallback_count(messages) -> int:
    """Fallback: count text tokens via heuristic, estimate images at fixed rate.

    This is used when:
    1. Gemini countTokens API fails
    2. Running with non-Gemini models (Claude, GPT, DeepSeek)

    Key fix: Images are estimated at 258-1000 tokens (actual Gemini rates) instead of
    LangChain's default which uses repr() and divides by 4, overcounting by 100x+.
    """
    count = 0
    for msg in messages:
        if isinstance(msg.content, str):
            count += len(msg.content) // 4
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        count += len(block.get("text", "")) // 4
                    elif block.get("type") in ("image_url", "media"):
                        count += 1000  # conservative estimate for one image/media
        count += 3  # per-message overhead
    return count
