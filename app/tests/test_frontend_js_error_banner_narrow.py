"""The error row's "Read more" and "Show Leo" must never overlap on a narrow panel.

Source-level assertions on style.css (layout itself needs a real browser; the
manual check is in docs/test_plans/0.7.14.md). The bug: "Read more" carried
`min-width: 0`, so on a narrow chat panel flexbox squeezed the button to zero
width while its text kept painting — right underneath "Show Leo". And the row
never wrapped, so the right end spilled past the panel edge.
"""

import re
from pathlib import Path

STYLE_CSS = (Path(__file__).resolve().parents[1] / "frontend" / "style.css").read_text()


def _rule(selector: str) -> str:
    match = re.search(r"(?m)^" + re.escape(selector) + r" \{([^}]*)\}", STYLE_CSS)
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_row_wraps_instead_of_spilling_past_the_panel():
    banner = _rule(".js-error-banner")
    assert "flex-wrap: wrap;" in banner
    # A gap between wrapped lines too, not just between items on one line.
    assert "row-gap:" in banner


def test_read_more_keeps_its_own_width():
    more = _rule(".js-error-banner .js-error-more")
    assert "flex-shrink: 0;" in more
    assert "white-space: nowrap;" in more


def test_show_leo_and_dismiss_keep_their_own_width():
    assert "flex-shrink: 0;" in _rule(".js-error-show")
    assert "flex-shrink: 0;" in _rule(".js-error-banner .js-error-x")


def test_summary_text_is_what_gives_way():
    # The summary is the one item allowed to shrink — it truncates with an
    # ellipsis (full text stays in the row) rather than pushing controls around.
    count = _rule(".js-error-count")
    assert "min-width: 0;" in count
    assert "text-overflow: ellipsis;" in count
