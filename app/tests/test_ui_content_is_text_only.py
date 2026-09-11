"""The browser never receives a raw provider content item (0.7.9).

Second layer of the ChatGPT/Codex `function_call` leak. The root cause was in
`message_invariants` — the history handed to the model was corrupted, so the
model imitated it — but the websocket had two different content policies and
only one of them filtered:

  * the streaming branch reduced list content to text blocks, and
  * the three `updates`-stream branches shipped `msg.content` to the browser
    verbatim, whatever was in it.

So even with the model behaving, one stray provider item upstream could still
land in a chat bubble. This pins the filter that closes that path.

The frontend normalisers (`MessageHandler.normalizeLLMStreamingContent`,
`ThreadManager.normalizeHistoricalMessageContent`) already drop non-text blocks.
They are fine — they just should not be the only guard.
"""
from langchain_core.messages import AIMessage

from app.websocket.request_handler import ui_text_content


# One assistant turn off the Responses API, as stored on the mothership.
RESPONSES_TURN = [
    {
        "id": "rs_0c3a3741",
        "type": "reasoning",
        "summary": [{"index": 0, "type": "summary_text", "text": "**Planning**"}],
        "content": [],
        "encrypted_content": "gAAAAABqou3-7D88aT",
    },
    {
        "type": "function_call",
        "name": "ask_user_question",
        "arguments": '{"questions":[{"question":"Build it?"}]}',
        "call_id": "call_Uvjmavg53bV62DZ0wUnVG72a",
        "id": "fc_0c3a3741",
        "index": 1,
    },
]


def test_a_function_call_item_never_reaches_the_browser():
    """The exact bubble the user saw."""
    out = ui_text_content(AIMessage(content=list(RESPONSES_TURN)))

    assert "function_call" not in str(out)
    assert "ask_user_question" not in str(out)


def test_reasoning_is_not_shown_as_assistant_text_either():
    """Thinking has its own channel; it must not arrive as the reply."""
    out = ui_text_content(AIMessage(content=list(RESPONSES_TURN)))

    assert "encrypted_content" not in str(out)
    assert "Planning" not in str(out)


def test_the_text_block_is_kept():
    msg = AIMessage(content=[
        {"type": "reasoning", "encrypted_content": "x"},
        {"type": "text", "text": "Here is the plan."},
        {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "c"},
    ])
    out = ui_text_content(msg)

    assert any(
        isinstance(b, dict) and b.get("type") == "text" and b["text"] == "Here is the plan."
        for b in out
    )


def test_a_tool_call_only_turn_sends_nothing_rather_than_json():
    """No text to show is an empty bubble, not a JSON bubble."""
    out = ui_text_content(AIMessage(content=[
        {"type": "function_call", "name": "f", "arguments": "{}", "call_id": "c"},
    ]))

    assert out == []


def test_plain_string_content_is_passed_straight_through():
    """The overwhelmingly common case must be byte-identical to before."""
    assert ui_text_content(AIMessage(content="just words")) == "just words"


def test_empty_string_content_stays_empty_string():
    assert ui_text_content(AIMessage(content="")) == ""


def test_a_message_with_no_content_attribute_is_stringified():
    """Matches the old `str(msg)` fallback for anything that isn't a message."""
    assert ui_text_content(object()) != ""


# ---------------------------------------------------------------------------
# What the mothership stores
# ---------------------------------------------------------------------------
#
# `report_message(content=str(...))` stored the Python repr of whatever the UI
# payload held. With a list that is `"[{'type': 'text', 'text': 'hi'}]"` — which
# is what made assistant content unreadable in /admin/message_annotations and in
# the eval miners.

from app.websocket.request_handler import plain_text_for_report


def test_reported_content_is_readable_text_not_a_python_repr():
    reported = plain_text_for_report([
        {"type": "text", "text": "Here is the plan."},
    ])

    assert reported == "Here is the plan."
    assert "{" not in reported and "'type'" not in reported


def test_multiple_text_blocks_are_joined():
    reported = plain_text_for_report([
        {"type": "text", "text": "First."},
        {"type": "text", "text": "Second."},
    ])

    assert reported == "First.\n\nSecond."


def test_a_plain_string_is_unchanged():
    assert plain_text_for_report("just words") == "just words"


def test_no_text_reports_empty_rather_than_brackets():
    assert plain_text_for_report([]) == ""
