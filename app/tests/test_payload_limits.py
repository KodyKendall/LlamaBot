"""Bounded ingestion of WebSocket frames (SupportIncident #246).

A Rails page that inlines a lot of content (meeting transcripts, long tables)
answers the `get_debug_info` postMessage with a `full_html` of the WHOLE rendered
document — 10.6 MB on the reported box — and the element picker puts an entire
`outerHTML` (375 KB) inside the message text. Both were copied into LangGraph
state verbatim on EVERY turn, so every checkpoint carried them and compaction had
nothing it could reclaim.

These tests pin the general property, not the two known fields: **nothing a client
sends becomes an unbounded state value**. The frontend caps can be bypassed (and
older boxes ship older frontends); this backstop cannot.
"""
import json

import pytest
from fastapi import FastAPI

from app.websocket.payload_limits import (
    DEBUG_INFO_VALUE_MAX_BYTES,
    MESSAGE_TEXT_MAX_BYTES,
    SELECTED_ELEMENT_MAX_BYTES,
    STATE_VALUE_MAX_BYTES,
    TRUNCATION_MARKER_RE,
    cap_debug_info,
    cap_message_text,
    cap_state_value,
    serialized_size,
)
from app.websocket.request_handler import RequestHandler


@pytest.fixture
def handler():
    return RequestHandler(FastAPI())


def _fat_html(size_bytes: int) -> str:
    """Realistic-ish rendered page markup of roughly `size_bytes`."""
    unit = "<details><summary>Meeting 2026-05-01</summary><p>transcript line</p></details>"
    return "<!DOCTYPE html><html><body>" + unit * (size_bytes // len(unit) + 1) + "</body></html>"


def _selected_element(size_bytes: int) -> str:
    """The exact shape the element picker sends."""
    filler = "<div class='row'>a transcript row</div>" * (size_bytes // 40 + 1)
    return f"<SELECTED_ELEMENT>\n<section class='transcripts'>{filler}</section>\n</SELECTED_ELEMENT>"


# ---------------------------------------------------------------------------
# Repro B — ingestion accepts an unbounded debug_info
# ---------------------------------------------------------------------------

class TestDebugInfoCap:
    def test_ten_megabyte_full_html_is_capped(self, caplog):
        debug_info = {
            "full_html": _fat_html(10 * 1024 * 1024),
            "view_path": "app/views/conversations/index.html.erb",
            "request_path": "/conversations",
        }
        with caplog.at_level("WARNING"):
            capped = cap_debug_info(debug_info)

        assert serialized_size(capped["full_html"]) <= DEBUG_INFO_VALUE_MAX_BYTES
        assert TRUNCATION_MARKER_RE.search(capped["full_html"])
        # A WARNING naming the key and both sizes — this is the operator's signal.
        warning = "\n".join(r.message for r in caplog.records if r.levelname == "WARNING")
        assert "full_html" in warning

    def test_the_keys_the_agents_actually_read_survive_intact(self):
        """view_path / request_path must never be collateral damage of the cap."""
        capped = cap_debug_info({
            "full_html": _fat_html(10 * 1024 * 1024),
            "view_path": "app/views/conversations/index.html.erb",
            "request_path": "/conversations",
        })
        assert capped["view_path"] == "app/views/conversations/index.html.erb"
        assert capped["request_path"] == "/conversations"

    def test_small_debug_info_is_untouched(self):
        debug_info = {"full_html": "<html>hi</html>", "view_path": "a.erb"}
        assert cap_debug_info(debug_info) == debug_info

    def test_nested_and_unexpected_shapes_are_bounded_too(self):
        """The NEXT unpredicted content-heavy field is caught by the same rule."""
        debug_info = {
            "page_context": {"body": _fat_html(2 * 1024 * 1024)},
            "console_log": [_fat_html(1024 * 1024) for _ in range(3)],
        }
        capped = cap_debug_info(debug_info)
        assert serialized_size(capped) <= STATE_VALUE_MAX_BYTES

    def test_non_dict_debug_info_does_not_explode(self):
        assert cap_debug_info(None) is None
        assert cap_debug_info("just a string") == "just a string"


class TestIngestionStateFields(object):
    """The real chokepoint: request_handler's pass-through loop."""

    def test_state_debug_info_is_capped(self, handler, caplog):
        message = {
            "message": "Keep transcription collapsed by default",
            "agent_name": "rails_agent",
            "thread_id": "48cbbf19-876f-47db-8514-b5a8ce927d15",
            "debug_info": {
                "full_html": _fat_html(10 * 1024 * 1024),
                "view_path": "app/views/conversations/index.html.erb",
                "request_path": "/conversations",
            },
        }
        with caplog.at_level("WARNING"):
            fields = handler._bounded_state_fields(message)

        assert "debug_info" in fields
        assert serialized_size(fields["debug_info"]["full_html"]) <= DEBUG_INFO_VALUE_MAX_BYTES
        assert fields["debug_info"]["view_path"].endswith("index.html.erb")
        assert "debug_info" in "\n".join(r.message for r in caplog.records)

    def test_routing_fields_are_not_passed_through(self, handler):
        fields = handler._bounded_state_fields({
            "message": "hi", "agent_name": "rails_agent",
            "thread_id": "t1", "attachments": [{"data": "x"}],
            "llm_model": "deepseek-v4-flash",
        })
        assert set(fields) == {"llm_model"}

    # -- Repro / 4.5: the regression guard for the whole class ---------------

    @pytest.mark.parametrize("key,value", [
        ("some_future_field", "x" * (5 * 1024 * 1024)),
        ("nested_blob", {"a": {"b": "x" * (5 * 1024 * 1024)}}),
        ("list_of_blobs", ["x" * (1024 * 1024)] * 8),
        ("page_context", {"full_html": "x" * (9 * 1024 * 1024)}),
    ])
    def test_no_state_value_exceeds_the_cap_whatever_the_key(self, handler, key, value):
        fields = handler._bounded_state_fields({
            "message": "hi", "agent_name": "rails_agent", "thread_id": "t1",
            key: value,
        })
        for k, v in fields.items():
            assert serialized_size(v) <= STATE_VALUE_MAX_BYTES, f"{k} came through unbounded"

    def test_ordinary_small_fields_are_passed_through_unchanged(self, handler):
        message = {
            "message": "hi", "agent_name": "rails_agent", "thread_id": "t1",
            "llm_model": "deepseek-v4-flash",
            "agent_mode": "engineer",
            "ask_before_edits": False,
            "api_token": "abc123",
            "debug_info": {"view_path": "a.erb", "request_path": "/"},
        }
        fields = handler._bounded_state_fields(message)
        assert fields["llm_model"] == "deepseek-v4-flash"
        assert fields["agent_mode"] == "engineer"
        assert fields["ask_before_edits"] is False
        assert fields["api_token"] == "abc123"
        assert fields["debug_info"] == {"view_path": "a.erb", "request_path": "/"}


# ---------------------------------------------------------------------------
# Repro C — oversized message text is capped
# ---------------------------------------------------------------------------

class TestMessageTextCap:
    def test_selected_element_block_is_middle_truncated_with_a_marker(self):
        text = "Keep transcription collapsed by default\n\n" + _selected_element(375 * 1024)
        capped = cap_message_text(text)

        assert serialized_size(capped) <= MESSAGE_TEXT_MAX_BYTES
        marker = TRUNCATION_MARKER_RE.search(capped)
        assert marker, "the agent must be told it is seeing a partial element"
        assert int(marker.group(1)) > 0
        # The user's own words and the element framing survive.
        assert "Keep transcription collapsed by default" in capped
        assert "<SELECTED_ELEMENT>" in capped and "</SELECTED_ELEMENT>" in capped
        assert "<section class='transcripts'>" in capped

    def test_small_message_is_untouched(self):
        text = "delete this button\n\n<SELECTED_ELEMENT>\n<button>Go</button>\n</SELECTED_ELEMENT>"
        assert cap_message_text(text) == text

    def test_oversized_text_without_any_selected_element_is_still_capped(self):
        capped = cap_message_text("please fix this: " + _fat_html(2 * 1024 * 1024))
        assert serialized_size(capped) <= MESSAGE_TEXT_MAX_BYTES
        assert capped.startswith("please fix this: ")

    def test_selected_element_alone_is_bounded_by_its_own_tighter_cap(self):
        capped = cap_message_text(_selected_element(375 * 1024))
        body = capped.split("<SELECTED_ELEMENT>", 1)[1]
        assert serialized_size(body) <= SELECTED_ELEMENT_MAX_BYTES + 512  # + marker slack

    def test_unterminated_selected_element_still_bounded(self):
        text = "look at this\n<SELECTED_ELEMENT>\n" + _fat_html(1024 * 1024)
        assert serialized_size(cap_message_text(text)) <= MESSAGE_TEXT_MAX_BYTES

    def test_non_string_input_is_returned_unchanged(self):
        assert cap_message_text(None) is None
        assert cap_message_text(123) == 123


class TestBuildMessageContentCap:
    """The cap has to live in the real path, not just in the helper."""

    def test_plain_text_message_is_capped(self, handler):
        message = {
            "message": "Keep transcription collapsed\n\n" + _selected_element(375 * 1024),
            "llm_model": "deepseek-v4-flash",
        }
        content = handler._build_message_content(message)
        assert isinstance(content, str)
        assert serialized_size(content) <= MESSAGE_TEXT_MAX_BYTES
        assert TRUNCATION_MARKER_RE.search(content)

    def test_multimodal_text_block_is_capped(self, handler, monkeypatch):
        monkeypatch.setenv("VISION_MODEL_ALLOWED", "true")
        img = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        content = handler._build_message_content({
            "message": "what about this?\n\n" + _selected_element(375 * 1024),
            "llm_model": "gemini-3-flash",
            "attachments": [{"mime_type": "image/png", "data": img, "filename": "s.png"}],
        })
        text = next(b for b in content if b.get("type") == "text")
        assert serialized_size(text["text"]) <= MESSAGE_TEXT_MAX_BYTES
        # The attachment itself is untouched by the text cap.
        assert any(b.get("type") == "image_url" for b in content)


class TestFrameLogging:
    """`_redact_frame` masked credentials but never truncated — so the same
    10.6 MB was also written to the docker logs on every message."""

    def test_oversized_frame_values_are_truncated_in_the_log_copy(self):
        from app.websocket.web_socket_handler import _redact_frame

        logged = _redact_frame({
            "message": "hi",
            "api_token": "secret",
            "debug_info": {"full_html": _fat_html(10 * 1024 * 1024)},
        })
        assert logged["api_token"] == "<redacted>"
        assert logged["message"] == "hi"
        assert serialized_size(str(logged)) < 64 * 1024

    def test_non_dict_frame_is_returned_unchanged(self):
        from app.websocket.web_socket_handler import _redact_frame

        assert _redact_frame("not a frame") == "not a frame"


class TestCapStateValuePrimitives:
    def test_scalars_pass_through(self):
        for v in (1, 1.5, True, False, None):
            assert cap_state_value("k", v) is v

    def test_unserializable_objects_do_not_raise(self):
        class Weird:
            def __repr__(self):
                return "x" * (2 * 1024 * 1024)

        capped = cap_state_value("weird", Weird())
        assert serialized_size(capped) <= STATE_VALUE_MAX_BYTES

    def test_capped_value_stays_json_serializable(self):
        capped = cap_state_value("debug_info", {"full_html": _fat_html(3 * 1024 * 1024)})
        json.dumps(capped)  # must not raise
