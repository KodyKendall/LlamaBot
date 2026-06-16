"""
Tests for StripUnsupportedMultimodalMiddleware.

Regression coverage for the bug where switching from a vision model (e.g.
Gemini) to a text-only model (DeepSeek) mid-thread caused a 400 from DeepSeek:

    Failed to deserialize the JSON body into the target type:
    messages[36]: unknown variant `image_url`, expected `text`

The image lived in *replayed history*, so per-message attachment gating at
send time wasn't enough — the whole message list has to be scrubbed before the
LLM call when the active model can't see images / video / PDFs.
"""
import pytest
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from app.agents.leonardo.rails_agent.middleware import (
    StripUnsupportedMultimodalMiddleware,
)

mw = StripUnsupportedMultimodalMiddleware()


def _img_block():
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANS"},
    }


def _history_with_image():
    return [
        SystemMessage(content="You are a helpful assistant."),
        HumanMessage(content=[
            {"type": "text", "text": "What's in this picture?"},
            _img_block(),
        ]),
        AIMessage(content="It's a cat."),
        HumanMessage(content="Now switch models and continue."),
    ]


def _contains_image_url(messages) -> bool:
    for m in messages:
        if isinstance(m.content, list):
            for block in m.content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def test_strips_image_url_for_text_only_model():
    """DeepSeek (no vision) must not receive any image_url blocks."""
    out = mw._strip_unsupported(_history_with_image(), "deepseek-v4-flash")
    assert not _contains_image_url(out), "image_url block leaked to a text-only model"


def test_replaces_stripped_image_with_explanatory_text():
    """The dropped image becomes a note the LLM can reason about, not a silent gap."""
    out = mw._strip_unsupported(_history_with_image(), "deepseek-v4-flash")
    human = out[1]
    text = human.content if isinstance(human.content, str) else " ".join(
        b.get("text", "") for b in human.content if isinstance(b, dict)
    )
    assert "image" in text.lower() and "removed" in text.lower()
    # Original text is preserved alongside the note.
    assert "What's in this picture?" in text


def test_preserves_original_text_block():
    out = mw._strip_unsupported(_history_with_image(), "deepseek-v4-flash")
    assert any(
        "What's in this picture?" in (m.content if isinstance(m.content, str) else "")
        or (
            isinstance(m.content, list)
            and any(
                isinstance(b, dict) and "What's in this picture?" in b.get("text", "")
                for b in m.content
            )
        )
        for m in out
    )


def test_vision_model_passes_through_unchanged():
    """Gemini can see images — history must be left exactly as-is."""
    history = _history_with_image()
    out = mw._strip_unsupported(history, "gemini-3-flash")
    assert _contains_image_url(out)
    assert out is history or out == history


def test_string_content_messages_untouched():
    """Plain-text messages should never be rewritten."""
    history = [HumanMessage(content="just text, no media")]
    out = mw._strip_unsupported(history, "deepseek-v4-flash")
    assert out[0].content == "just text, no media"


def test_collapses_to_string_when_only_text_remains():
    """A list that's just one text block after stripping should collapse to a str."""
    history = [HumanMessage(content=[_img_block()])]
    out = mw._strip_unsupported(history, "deepseek-v4-flash")
    # Only the placeholder text remains; simplest valid shape is a plain string.
    assert isinstance(out[0].content, str)
    assert "image" in out[0].content.lower()
