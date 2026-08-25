"""Shaping Rails crash-feed entries for the chat page's error tray.

The tray is a one-line notice above the composer that already carries the
preview's JavaScript errors. These are the server-side half: the same button
press that renders a 500 should put a line there too, instead of the user
staring at a Rails error page wondering whether Leo can see it.

What matters here is that nothing the Rails app can put in an exception message
can break the browser contract: entries with no usable text are dropped rather
than rendered blank, the backtrace is trimmed to something a popup can hold, and
the list is capped so a crash loop cannot hand the page a megabyte of stack.
"""

from app.lib import rails_error_tray as tray


def _entry(**overrides):
    base = {
        "seq": 7,
        "fingerprint": "abc123",
        "error_class": "NoMethodError",
        "message": "undefined method `title' for nil",
        "method": "GET",
        "path": "/posts/1",
        "count": 1,
        "at": "2026-08-23T10:00:00Z",
        "backtrace": ["app/views/posts/show.html.erb:3", "app/controllers/posts_controller.rb:8"],
    }
    base.update(overrides)
    return base


def test_entry_carries_what_the_tray_renders():
    shaped = tray.tray_entry(_entry())

    assert shaped == {
        "id": "rails-7",
        "kind": "rails",
        "message": "NoMethodError: undefined method `title' for nil",
        "path": "GET /posts/1",
        "count": 1,
        "stack": "app/views/posts/show.html.erb:3\napp/controllers/posts_controller.rb:8",
    }


def test_path_omits_a_missing_method():
    assert tray.tray_entry(_entry(method=None))["path"] == "/posts/1"


def test_path_is_blank_when_rails_recorded_none():
    # Background jobs crash with no request at all. The tray renders the error
    # without a "on <path>" line rather than inventing one.
    assert tray.tray_entry(_entry(method=None, path=None))["path"] == ""


def test_message_falls_back_to_the_class_alone():
    shaped = tray.tray_entry(_entry(message=""))
    assert shaped["message"] == "NoMethodError"


def test_entry_with_no_text_at_all_is_dropped():
    # Rendering "" in the tray would tell the user something broke and refuse to
    # say what. Better to have never counted it.
    assert tray.tray_entry(_entry(error_class="", message="")) is None
    assert tray.tray_entry("not a dict") is None


def test_repeat_count_is_carried_through():
    assert tray.tray_entry(_entry(count=42))["count"] == 42


def test_junk_count_reads_as_one():
    assert tray.tray_entry(_entry(count="lots"))["count"] == 1
    assert tray.tray_entry(_entry(count=0))["count"] == 1


def test_long_message_is_truncated():
    shaped = tray.tray_entry(_entry(message="x" * 5000))
    assert len(shaped["message"]) <= tray.MAX_MESSAGE_CHARS + len("NoMethodError: ")


def test_backtrace_is_trimmed_to_the_frames_that_name_the_file():
    shaped = tray.tray_entry(_entry(backtrace=[f"frame{i}" for i in range(50)]))
    assert shaped["stack"].count("\n") + 1 == tray.MAX_BACKTRACE_LINES


def test_missing_backtrace_gives_no_stack():
    assert tray.tray_entry(_entry(backtrace=None))["stack"] is None
    assert tray.tray_entry(_entry(backtrace=[]))["stack"] is None


def test_entries_caps_the_list_and_keeps_the_newest():
    shaped = tray.tray_entries([_entry(seq=i, message=f"boom {i}") for i in range(40)])

    assert len(shaped) == tray.MAX_TRAY_ERRORS
    # The ring is oldest-first, so the tail is what just happened.
    assert shaped[-1]["message"].endswith("boom 39")


def test_entries_skips_unusable_rows_without_dropping_the_rest():
    shaped = tray.tray_entries([_entry(seq=1), {"seq": 2}, _entry(seq=3)])
    assert [e["id"] for e in shaped] == ["rails-1", "rails-3"]


def test_entries_tolerates_junk():
    assert tray.tray_entries(None) == []
    assert tray.tray_entries("nope") == []
    assert tray.tray_entries([None, 5, "x"]) == []
