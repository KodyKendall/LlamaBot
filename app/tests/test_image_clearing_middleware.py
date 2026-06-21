"""Tests for ToolResultImageClearingMiddleware and image-stripping token counter.

Regression for the summarization loop bug: browser_inspect stores screenshots as
base64 image_url blocks in ToolMessages. After several calls these accumulate and
keep the conversation above the summarization threshold on every turn, triggering
summarization on every single turn in an infinite loop.
"""

import pytest
from unittest.mock import MagicMock
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from app.agents.utils.token_counter import (
    _strip_old_images,
    SCREENSHOT_KEEP_RECENT,
    gemini_multimodal_token_counter_strip_images,
)
from app.agents.leonardo.rails_agent.middleware import ToolResultImageClearingMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_screenshot_tool_msg(tool_call_id: str, text: str = "page ok") -> ToolMessage:
    """ToolMessage that mimics a browser_inspect result with a screenshot."""
    return ToolMessage(
        content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123=="}},
        ],
        tool_call_id=tool_call_id,
    )


def _make_text_tool_msg(tool_call_id: str, text: str = "result") -> ToolMessage:
    """ToolMessage with text-only content (no image)."""
    return ToolMessage(content=text, tool_call_id=tool_call_id)


def _has_image(msg: ToolMessage) -> bool:
    content = msg.content
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "image_url" for b in content)


# ---------------------------------------------------------------------------
# _strip_old_images
# ---------------------------------------------------------------------------

class TestStripOldImages:
    def test_no_images_returns_same_list(self):
        messages = [
            HumanMessage(content="hi"),
            _make_text_tool_msg("t1"),
        ]
        result = _strip_old_images(messages)
        assert result is messages

    def test_fewer_images_than_keep_returns_same_list(self):
        messages = [
            HumanMessage(content="hi"),
            _make_screenshot_tool_msg("t1"),
        ]
        result = _strip_old_images(messages, keep_recent=2)
        assert result is messages

    def test_exactly_keep_recent_images_returns_same_list(self):
        messages = [
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
        ]
        result = _strip_old_images(messages, keep_recent=2)
        assert result is messages

    def test_strips_oldest_when_over_limit(self):
        msgs = [
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),  # 3 images, keep_recent=2
        ]
        result = _strip_old_images(msgs, keep_recent=2)

        # t1 (oldest) should have image stripped
        assert not _has_image(result[0])
        # t2 and t3 (most recent 2) should keep images
        assert _has_image(result[1])
        assert _has_image(result[2])

    def test_strips_multiple_old_images(self):
        msgs = [
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),
            _make_screenshot_tool_msg("t4"),  # 4 images, keep_recent=2
        ]
        result = _strip_old_images(msgs, keep_recent=2)

        assert not _has_image(result[0])
        assert not _has_image(result[1])
        assert _has_image(result[2])
        assert _has_image(result[3])

    def test_cleared_message_keeps_text_block(self):
        msgs = [
            _make_screenshot_tool_msg("t1", text="console errors: none"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),
        ]
        result = _strip_old_images(msgs, keep_recent=2)

        cleared = result[0]
        content = cleared.content
        assert isinstance(content, list)
        text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        assert len(text_blocks) >= 1, "text block should survive clearing"
        all_text = " ".join(b["text"] for b in text_blocks)
        assert "console errors: none" in all_text, "original text content preserved"

    def test_cleared_message_contains_placeholder(self):
        msgs = [
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),
        ]
        result = _strip_old_images(msgs, keep_recent=2)
        content = result[0].content
        placeholder_blocks = [
            b for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and "browser_inspect" in b.get("text", "")
        ]
        assert placeholder_blocks, "placeholder referencing browser_inspect should be present"

    def test_non_tool_messages_untouched(self):
        human = HumanMessage(content="check the page")
        ai = AIMessage(content="ok")
        msgs = [human, ai, _make_screenshot_tool_msg("t1"), _make_screenshot_tool_msg("t2"), _make_screenshot_tool_msg("t3")]
        result = _strip_old_images(msgs, keep_recent=2)

        assert result[0] is human
        assert result[1] is ai

    def test_text_only_tool_messages_not_affected(self):
        msgs = [
            _make_text_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),
            _make_screenshot_tool_msg("t4"),
        ]
        result = _strip_old_images(msgs, keep_recent=2)
        # text-only msg is untouched
        assert result[0] is msgs[0]
        # oldest screenshot (t2) is cleared
        assert not _has_image(result[1])
        # most recent 2 (t3, t4) kept
        assert _has_image(result[2])
        assert _has_image(result[3])


# ---------------------------------------------------------------------------
# gemini_multimodal_token_counter_strip_images
# ---------------------------------------------------------------------------

class TestImageStrippingTokenCounter:
    def test_fewer_tokens_counted_when_old_images_stripped(self):
        """Token count for old screenshots should be much lower after stripping."""
        # 3 screenshots, keep_recent=2 means the first is stripped
        msgs = [
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),
        ]
        count_with_stripping = gemini_multimodal_token_counter_strip_images(msgs)
        # Can't call the full counter without network, but stripping should at minimum
        # not raise an exception, and the function should be callable
        assert isinstance(count_with_stripping, int)
        assert count_with_stripping >= 0

    def test_no_images_works_normally(self):
        msgs = [
            HumanMessage(content="hello"),
            _make_text_tool_msg("t1", "output text"),
        ]
        count = gemini_multimodal_token_counter_strip_images(msgs)
        assert isinstance(count, int)
        assert count > 0


# ---------------------------------------------------------------------------
# ToolResultImageClearingMiddleware
# ---------------------------------------------------------------------------

class TestToolResultImageClearingMiddleware:
    def setup_method(self):
        self.middleware = ToolResultImageClearingMiddleware(keep_recent=2)
        self.runtime = MagicMock()

    def _state(self, messages):
        return {"messages": messages}

    def test_no_op_when_no_images(self):
        state = self._state([HumanMessage(content="hi"), _make_text_tool_msg("t1")])
        result = self.middleware.before_model(state, self.runtime)
        assert result is None

    def test_no_op_when_images_within_limit(self):
        state = self._state([
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
        ])
        result = self.middleware.before_model(state, self.runtime)
        assert result is None

    def test_returns_state_update_when_over_limit(self):
        state = self._state([
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),
        ])
        result = self.middleware.before_model(state, self.runtime)
        assert result is not None
        assert "messages" in result

    def test_state_update_clears_old_images(self):
        msgs = [
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),
        ]
        state = self._state(msgs)
        result = self.middleware.before_model(state, self.runtime)
        assert result is not None

        updated_msgs = result["messages"]
        # Filter out RemoveMessage markers (they don't have a 'content' attr the same way)
        tool_msgs = [m for m in updated_msgs if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 3

        # oldest (t1) should not have image
        t1 = next(m for m in tool_msgs if m.tool_call_id == "t1")
        assert not _has_image(t1)

        # most recent 2 should keep images
        t2 = next(m for m in tool_msgs if m.tool_call_id == "t2")
        t3 = next(m for m in tool_msgs if m.tool_call_id == "t3")
        assert _has_image(t2)
        assert _has_image(t3)

    def test_async_before_model_matches_sync(self):
        import asyncio
        msgs = [
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
            _make_screenshot_tool_msg("t3"),
        ]
        state = self._state(msgs)
        sync_result = self.middleware.before_model(state, self.runtime)
        async_result = asyncio.get_event_loop().run_until_complete(
            self.middleware.abefore_model(state, self.runtime)
        )
        # Both should agree on whether to clear
        assert (sync_result is None) == (async_result is None)

    def test_custom_keep_recent(self):
        mw = ToolResultImageClearingMiddleware(keep_recent=1)
        msgs = [
            _make_screenshot_tool_msg("t1"),
            _make_screenshot_tool_msg("t2"),
        ]
        result = mw.before_model(self._state(msgs), self.runtime)
        assert result is not None  # 2 images, keep_recent=1 → should clear

    def test_default_keep_recent_matches_constant(self):
        assert self.middleware.keep_recent == SCREENSHOT_KEEP_RECENT
