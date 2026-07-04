"""Tests for the authoritative backend vision gate in
``RequestHandler._build_message_content``.

``VISION_MODEL_ALLOWED`` is the operator opt-in for image/video understanding.
When it is off, visual attachments must never be turned into ``image_url`` /
``file`` content blocks — regardless of the selected model's own capabilities —
and the user gets a support hand-off note instead. The frontend blocks the send
too, but this is the real chokepoint since ``llm_model`` / ``attachments`` are
unvalidated websocket input.
"""
import pytest
from fastapi import FastAPI

from app.websocket.request_handler import RequestHandler, SUPPORT_EMAIL


# A 1x1 base64 blob — content is irrelevant, only that it's a non-empty image.
_IMG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


@pytest.fixture
def handler():
    return RequestHandler(FastAPI())


def _image_message(model="gemini-3-flash"):
    return {
        "message": "what is in this image?",
        "llm_model": model,
        "attachments": [
            {"mime_type": "image/png", "data": _IMG, "filename": "shot.png"},
        ],
    }


def _blocks_of_type(content, block_type):
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == block_type]


def test_vision_off_drops_image_and_adds_support_note(handler, monkeypatch):
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "false")
    content = handler._build_message_content(_image_message())

    # No image block reaches the LLM even though gemini supports images.
    assert _blocks_of_type(content, "image_url") == []
    # The user's text survives, with a support hand-off appended.
    text = " ".join(b["text"] for b in _blocks_of_type(content, "text"))
    assert "what is in this image?" in text
    assert SUPPORT_EMAIL in text


def test_vision_on_passes_image_through(handler, monkeypatch):
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
    content = handler._build_message_content(_image_message())

    images = _blocks_of_type(content, "image_url")
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # No support note when vision is enabled.
    text = " ".join(b["text"] for b in _blocks_of_type(content, "text"))
    assert SUPPORT_EMAIL not in text


def test_vision_off_leaves_plain_text_untouched(handler, monkeypatch):
    """No attachments -> plain string, unchanged, regardless of the gate."""
    monkeypatch.setenv("VISION_MODEL_ALLOWED", "false")
    content = handler._build_message_content({"message": "hello", "llm_model": "deepseek-v4-flash"})
    assert content == "hello"
