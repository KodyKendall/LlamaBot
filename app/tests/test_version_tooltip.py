"""Regression tests for the "hover Leo → see the version number" tooltip.

The Leonardo title (`<h1 data-llamabot="leonardo-title">Leo</h1>`) gets a native
`title` attribute set to `v{version}` in chat.html, and style.css renders that as
a styled tooltip via `.chat-header h1[title]:hover::after { content: attr(title) }`.

This broke once when `overflow: hidden` was added to `.chat-header h1` (for flex
truncation): the `::after` tooltip is positioned *below* the title (top:100%), so
`overflow: hidden` clipped it and the version stopped showing on hover. These
tests lock the wiring in place so that regression can't silently come back.
"""
import re
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
CHAT_HTML = (FRONTEND_DIR / "chat.html").read_text()
STYLE_CSS = (FRONTEND_DIR / "style.css").read_text()


def _css_block(selector):
    """Return the body of the CSS rule whose comma-separated selector group
    contains `selector` as an *exact* entry.

    Matching the exact selector (not a substring) is essential: `.chat-header h1`
    also appears inside the collapsed `display:none` rule's selector list and
    inside `.chat-header h1[title]:hover::after`, and we must not pick those up.
    """
    pattern = re.compile(r"(?P<sel>[^{}]*?)\{(?P<body>[^{}]*)\}")
    for m in pattern.finditer(STYLE_CSS):
        selectors = {s.strip() for s in m.group("sel").split(",")}
        if selector in selectors:
            return m.group("body")
    raise AssertionError(f"No CSS rule found with exact selector {selector!r}")


class TestVersionTooltipWiring:
    def test_title_element_present(self):
        assert 'data-llamabot="leonardo-title"' in CHAT_HTML

    def test_title_attribute_set_to_version(self):
        # The JS must populate the native title attribute with the version string,
        # which is the content the CSS tooltip mirrors.
        assert re.search(
            r"leonardoTitle\.title\s*=\s*[`'\"]v\$\{version\}", CHAT_HTML
        ), "chat.html must set the leonardo title attribute to `v${version}`"

    def test_hover_tooltip_rule_present(self):
        # The styled tooltip rule that renders the title on hover.
        assert ".chat-header h1[title]:hover::after" in STYLE_CSS
        after_body = _css_block(".chat-header h1[title]:hover::after")
        assert "attr(title)" in after_body, (
            "the hover tooltip must render the title attribute via content: attr(title)"
        )


class TestVersionTooltipNotClipped:
    def test_title_does_not_clip_overflow(self):
        """`.chat-header h1` must not set overflow:hidden — it would clip the
        `::after` version tooltip positioned below the title (top:100%)."""
        body = _css_block(".chat-header h1")
        # Strip CSS comments so the explanatory NOTE (which mentions the phrase)
        # isn't mistaken for a real declaration.
        declarations = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        assert not re.search(r"overflow\s*:\s*hidden", declarations), (
            "overflow:hidden on .chat-header h1 clips the version tooltip "
            "(the ::after is positioned below the title); remove it."
        )
