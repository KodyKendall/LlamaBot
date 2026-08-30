"""Feedback must identify the rated message by a key, not by comparing its text.

Mothership annotation #688 (rsb-dev, 2026-08-28): the chat socket dropped mid-run, so the
browser held only the last 435 characters of a complete 906-character answer. The thumbs-down
sent that DOM text as `content`, and the mothership's resolve_feedback_message matches on
exact string equality — it matched nothing, fell through to build_placeholder_message, and
CREATED a new instance_messages row from the partial text. The annotation now points at a
fabricated fragment with no model, no tokens and no tool calls, while the real message shows
as unrated.

17 of 424 end-user annotations in the last 30 days (4%) landed on such a row, and the bias is
not random: a broken stream is precisely what causes the mismatch. We are systematically
losing the feedback on our worst runs.

The fix is a stable key on the assistant turn, sent by BOTH report_message and
submit_feedback, so the mothership can join exactly. `content` keeps being sent so older
mothership code goes on working.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.mothership_client import MothershipClient


def _client():
    client = MothershipClient()
    client.config = {
        "instance_name": "leo-test",
        "mothership_url": "https://llamapress.ai",
        "mothership_api_token": "token",
    }
    return client


def _captured_payload(mock_post):
    assert mock_post.await_count == 1, "expected exactly one POST"
    return mock_post.await_args.kwargs["json"]


@pytest.fixture
def post_ok():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"ok": True})

    with patch("httpx.AsyncClient") as ac:
        instance = ac.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=response)
        yield instance.post


def test_report_message_sends_the_message_key(post_ok):
    client = _client()
    with patch.object(MothershipClient, "reporting_enabled", True):
        asyncio.run(client.report_message(
            thread_id="t1",
            role="assistant",
            content="the full 906 char answer",
            sent_at="2026-08-28T19:07:56Z",
            message_key="msg-abc123",
        ))

    assert _captured_payload(post_ok)["message_key"] == "msg-abc123"


def test_submit_feedback_sends_the_same_key(post_ok):
    client = _client()
    with patch.object(MothershipClient, "reporting_enabled", True):
        asyncio.run(client.submit_feedback(
            thread_id="t1",
            rating="bad",
            content="…the truncated 435 char tail",
            message_key="msg-abc123",
        ))

    payload = _captured_payload(post_ok)
    assert payload["message_key"] == "msg-abc123"


def test_content_is_still_sent_so_older_mothership_code_keeps_working(post_ok):
    client = _client()
    with patch.object(MothershipClient, "reporting_enabled", True):
        asyncio.run(client.submit_feedback(
            thread_id="t1",
            rating="bad",
            content="…the truncated 435 char tail",
            message_key="msg-abc123",
        ))

    payload = _captured_payload(post_ok)
    assert payload["content"] == "…the truncated 435 char tail"


def test_key_is_omitted_when_absent_rather_than_sent_as_null(post_ok):
    """A null key must not look like a real key the mothership can join on."""
    client = _client()
    with patch.object(MothershipClient, "reporting_enabled", True):
        asyncio.run(client.submit_feedback(thread_id="t1", rating="good"))

    assert "message_key" not in _captured_payload(post_ok)
