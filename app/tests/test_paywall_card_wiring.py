"""The paywall card must actually USE the plan-aware copy.

paywall_card_copy.test.mjs proves the copy is right; these pin that it reaches
the DOM. The failure mode this guards is the cheap one — someone re-inlines a
literal title into the template and the tested module quietly stops mattering.

The literals below are the exact strings that told a Pro annual customer he was
out of *free* messages (leo-rofme, 2026-09-05).
"""

from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "chat"
RENDERER = (FRONTEND / "messages" / "MessageRenderer.js").read_text()
HANDLER = (FRONTEND / "websocket" / "MessageHandler.js").read_text()


@pytest.mark.parametrize("literal", [
    "You've used your free messages for today",
    "Come back tomorrow or upgrade for more messages",
])
def test_the_old_hardcoded_copy_is_gone(literal):
    assert literal not in RENDERER, (
        f"{literal!r} is back in the template — a paying customer sees it"
    )


def test_the_renderer_asks_paywall_copy_for_its_words():
    assert "paywallCardCopy" in RENDERER
    assert "from './paywallCopy.js'" in RENDERER


def test_the_frame_is_passed_through_to_the_card():
    """block_reason / plan / resets_at are useless if the card never sees them."""
    assert "renderPaywallMessage(PAYWALL_UPGRADE_URL, data)" in HANDLER


def test_the_efficiency_link_is_on_the_card():
    assert "EFFICIENCY_WIKI_URL" in RENDERER
    assert "paywall-card-link" in RENDERER


def test_the_copy_is_set_as_text_not_html():
    """It carries a time rendered from a mothership payload, so it is not a
    trusted literal and must not be interpolated into innerHTML."""
    assert ".paywall-card-title').textContent" in RENDERER
    assert ".paywall-card-subtitle').textContent" in RENDERER


# ---------------------------------------------------------------------------
# The crash button's repeat-click guard is wired into the receiver
# ---------------------------------------------------------------------------

INDEX = (FRONTEND / "index.js").read_text()


def test_the_auto_send_guard_is_installed():
    assert "import { AutoSendGuard } from './utils/autoSendGuard.js';" in INDEX
    assert "this.autoSendGuard = new AutoSendGuard()" in INDEX


def test_auto_sends_go_through_the_guard():
    """The whole point: an auto_send frame is checked BEFORE it is sent."""
    assert "event.data.auto_send && !this.autoSendGuard.shouldSend(command)" in INDEX


def test_a_dropped_click_tells_the_user_something_happened():
    """Silence is what produced the second click. The customer must see that
    the first one registered."""
    guard_at = INDEX.index("this.autoSendGuard.shouldSend(command)")
    following = INDEX[guard_at:guard_at + 900]
    assert "renderSystemMessage" in following, following[:400]


def test_a_typed_message_is_not_deduped():
    """Only auto_send frames are gated — a human pressing send twice meant it."""
    assert "this.autoSendGuard.shouldSend" in INDEX
    assert INDEX.count("this.autoSendGuard.shouldSend") == 1
