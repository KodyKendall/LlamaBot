"""The "Leo can't view images" banner must be dismissable — and visibly so.

Source-level assertions (the banner has no isolated JS module to exercise): the
markup carries a dismiss button, its styling actually wins the cascade against
the generic composer-button rules, the banner appears whenever an image attach
is refused, and a dismissal lasts only until the next refusal.
"""

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
CHAT_HTML = (FRONTEND / "chat.html").read_text()
CHAT_JS = (FRONTEND / "chat" / "index.js").read_text()
STYLE_CSS = (FRONTEND / "style.css").read_text()


def _vision_banner_markup() -> str:
    start = CHAT_HTML.index('data-llamabot="vision-disabled-banner"')
    end = CHAT_HTML.index("</div>", CHAT_HTML.index("</div>", start) + 1)
    return CHAT_HTML[start:end]


def test_vision_banner_has_dismiss_button():
    markup = _vision_banner_markup()
    assert 'data-llamabot="vision-disabled-dismiss"' in markup
    assert 'aria-label="Dismiss"' in markup


def test_dismiss_button_style_outranks_the_generic_composer_button_rule():
    # The banner lives inside .input-area, whose `.input-area button` rule paints
    # every composer button translucent-white on faint purple — light gray on
    # light blue, which is the QA complaint. An UNSCOPED
    # `.image-switch-banner-dismiss` rule loses to it on specificity and does
    # nothing at all, so the dismiss rules must stay scoped under the banner.
    assert ".image-switch-banner .image-switch-banner-dismiss {" in STYLE_CSS
    assert "\n.image-switch-banner-dismiss {" not in STYLE_CSS
    # The hover half needs `:not(:disabled)` to outrank the equally-scoped
    # `.input-area button:hover:not(:disabled)` — a plain `:hover` loses.
    assert (
        ".image-switch-banner .image-switch-banner-dismiss:hover:not(:disabled) {"
        in STYLE_CSS
    )


def test_dismiss_button_is_legible_without_hovering():
    # QA twice: a blue × on a pale blue banner disappears into it, faded or not.
    # It must be inverted — a white glyph on a solid (never alpha-faded) fill.
    rules = STYLE_CSS[STYLE_CSS.index(".image-switch-banner .image-switch-banner-dismiss {"):]
    resting = rules[: rules.index("}")]
    assert "rgba(" not in resting, "the × must not be alpha-faded into the banner"
    assert "color: #ffffff" in resting
    assert "background: #0f172a" in resting

    hover = rules[rules.index(".image-switch-banner-dismiss:hover"):]
    hover = hover[: hover.index("}")]
    assert "background" in hover


def test_refusal_message_stays_in_the_inline_banner():
    # The message belongs above the composer, next to what the user just tried to
    # attach — not in a bottom-right toast on the far side of the screen.
    body = CHAT_JS[CHAT_JS.index("refuseImageAttachment() {"):]
    body = body[: body.index("\n  }")]
    assert "updateImageSwitchBanner()" in body
    assert "showToast" not in body


def test_screenshot_capture_respects_the_vision_gate():
    # initScreenshotCapture pushes straight into fileAttachmentManager.attachments,
    # so the manager-level gate can't see it — the click handler must check first.
    handler = CHAT_JS[CHAT_JS.index("initScreenshotCapture() {"):]
    handler = handler[: handler.index("startCapture(")]
    # visionUsable() folds in BOTH refusal causes (operator switched vision off,
    # and no vision-capable model on this box) — checking visionAllowed alone
    # would let a capture through on a box that has no model to read it.
    assert "!this.visionUsable()" in handler
    assert "this.refuseImageAttachment()" in handler


def test_banner_shows_when_an_image_attach_is_refused():
    # Vision off now blocks the attach outright, so the banner can no longer key
    # off "an image is attached" — it must also fire on a refused attach.
    toggle = CHAT_JS[CHAT_JS.index("const visionBlocked ="):]
    toggle = toggle[: toggle.index("const banner =")]
    assert "this.visionBlockNoticeActive" in toggle


def test_banner_stays_hidden_once_dismissed():
    # The visibility toggle must consult the dismissed flag, not just visionBlocked.
    toggle = CHAT_JS[CHAT_JS.index("const visionBlocked ="):]
    toggle = toggle[: toggle.index("const banner =")]
    assert "this.visionBannerDismissed" in toggle


def test_dismissal_is_not_remembered():
    # The banner is the only place a refused image is explained, so a dismissal
    # must not outlive the refusal that raised it: nothing is persisted, and the
    # next refusal clears the flag and shows the banner again.
    assert "VISION_BANNER_DISMISSED_KEY" not in CHAT_JS

    body = CHAT_JS[CHAT_JS.index("refuseImageAttachment() {"):]
    body = body[: body.index("\n  }")]
    assert "this.visionBannerDismissed = false" in body
