"""The "Your App is Building!" overlay's tip rotation.

The overlay is the one screen every customer watches, so it is where the two
things we most want people to know go (Darren, 2026-09-05): that a new chat per
topic makes their messages go further, and that they can bring their own ChatGPT
account. Both link out to wiki pages that must exist — a tip that 404s is worse
than no tip.
"""

import re
from pathlib import Path

import pytest

IFRAME_MANAGER = (
    Path(__file__).resolve().parents[1] / "frontend" / "chat" / "ui" / "IframeManager.js"
).read_text()

TIPS_BLOCK = re.search(r"const tips = \[(.*?)\n    \];", IFRAME_MANAGER, re.S)


def test_the_tips_array_is_still_where_we_think_it_is():
    assert TIPS_BLOCK, "the tips array moved — the assertions below are blind"


@pytest.mark.parametrize("fragment,href", [
    ("starting new chats on new topics",
     "https://llamapress.ai/wiki/using-your-messages-efficiently"),
    ("Connect your ChatGPT account",
     "https://llamapress.ai/wiki/use-your-chatgpt-plan-with-leo"),
])
def test_the_new_tips_are_present_and_linked(fragment, href):
    body = TIPS_BLOCK.group(1)
    assert fragment in body, f"tip missing: {fragment!r}"
    assert href in body, f"tip is not linked to {href}"


def test_every_tip_has_an_icon_and_text():
    """renderTip reads .icon and .text unconditionally; a tip missing either
    renders a blank pill mid-rotation."""
    entries = re.findall(r"\{[^{}]*\}", TIPS_BLOCK.group(1))
    assert len(entries) >= 6, entries
    for entry in entries:
        assert "icon:" in entry and "text:" in entry, entry


def test_no_tip_links_somewhere_that_is_not_the_wiki():
    for href in re.findall(r"href: '([^']+)'", TIPS_BLOCK.group(1)):
        assert href.startswith("https://llamapress.ai/wiki"), href
