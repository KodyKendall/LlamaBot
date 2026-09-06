"""The paywall frame must say WHY the turn was blocked, and on which plan.

Until 2026-09-05 the mothership short-circuited `paywall_status` to "allowed" for
anyone with a subscription, so only free users could ever be blocked and the card
could safely hardcode the word "free". That short-circuit is gone: paid plans now
have real per-plan daily caps, and a second, different block exists (a per-account
daily SPEND ceiling) for which "come back tomorrow or upgrade for more messages"
is simply the wrong sentence.

A Pro annual customer (leo-rofme, 2026-09-05) was told he was out of *free*
messages and opened the pricing page three times trying to work out what to buy.

The mothership already returns `block_reason`, `plan` and `resets_at` on both
`report_message` and `check_paywall`. These tests pin that the box carries them
through to the browser instead of dropping them, and that an OLDER mothership —
which sends none of them — still produces exactly today's frame.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketState

from app.websocket.request_handler import RequestHandler


def _connected_websocket():
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


def _handler(monkeypatch, *, cached, recheck):
    monkeypatch.setenv("PAYWALL_ENABLED", "true")
    handler = RequestHandler(MagicMock())
    handler.app.state.paywall_credits = dict(cached)
    mothership = MagicMock()
    mothership.check_paywall = AsyncMock(return_value=recheck)
    handler.app.state.mothership_client = mothership
    return handler


BLOCKED = {"allowed_next": False, "messages_remaining": 0}


@pytest.mark.asyncio
async def test_paywall_hit_carries_block_reason_plan_and_reset_time(monkeypatch):
    handler = _handler(monkeypatch, cached=BLOCKED, recheck={
        "allowed": False,
        "messages_remaining": 0,
        "block_reason": "message_limit",
        "plan": "pro",
        "resets_at": "2026-09-06T00:00:00-04:00",
    })
    websocket = _connected_websocket()

    assert await handler._check_paywall_or_block(websocket) is True

    frame = websocket.send_json.await_args_list[-1].args[0]
    assert frame["type"] == "paywall_hit"
    assert frame["block_reason"] == "message_limit"
    assert frame["plan"] == "pro"
    assert frame["resets_at"] == "2026-09-06T00:00:00-04:00"


@pytest.mark.asyncio
async def test_spend_limit_is_reported_as_itself(monkeypatch):
    """A spend ceiling is not a message count — the copy differs."""
    handler = _handler(monkeypatch, cached=BLOCKED, recheck={
        "allowed": False,
        "messages_remaining": 12,
        "block_reason": "spend_limit",
        "plan": "business",
    })
    websocket = _connected_websocket()

    await handler._check_paywall_or_block(websocket)

    frame = websocket.send_json.await_args_list[-1].args[0]
    assert frame["block_reason"] == "spend_limit"
    assert frame["messages_remaining"] == 12


@pytest.mark.asyncio
async def test_an_older_mothership_still_produces_todays_frame(monkeypatch):
    """Rollout safety: none of the new keys are invented on the box."""
    handler = _handler(
        monkeypatch, cached=BLOCKED,
        recheck={"allowed": False, "messages_remaining": 0},
    )
    websocket = _connected_websocket()

    await handler._check_paywall_or_block(websocket)

    frame = websocket.send_json.await_args_list[-1].args[0]
    assert frame == {"type": "paywall_hit", "messages_remaining": 0}


@pytest.mark.asyncio
async def test_the_frame_falls_back_to_what_report_message_cached(monkeypatch):
    """`report_message` learns the plan on every ALLOWED turn too, so a recheck
    that omits it must not downgrade a paying customer to the free copy."""
    handler = _handler(
        monkeypatch,
        cached={**BLOCKED, "plan": "starter", "block_reason": "message_limit",
                "resets_at": "2026-09-06T00:00:00-07:00"},
        recheck={"allowed": False, "messages_remaining": 0},
    )
    websocket = _connected_websocket()

    await handler._check_paywall_or_block(websocket)

    frame = websocket.send_json.await_args_list[-1].args[0]
    assert frame["plan"] == "starter"
    assert frame["resets_at"] == "2026-09-06T00:00:00-07:00"


@pytest.mark.asyncio
async def test_report_message_response_populates_the_cache(monkeypatch):
    """The plan is knowable before the block, so the card is right first time."""
    monkeypatch.setenv("PAYWALL_ENABLED", "true")
    handler = RequestHandler(MagicMock())
    handler.app.state.paywall_credits = {}
    mothership = MagicMock()
    mothership.report_message = AsyncMock(return_value={
        "allowed_next": True,
        "messages_remaining": 4,
        "plan": "pro",
        "resets_at": "2026-09-06T00:00:00-04:00",
    })
    handler.app.state.mothership_client = mothership
    websocket = _connected_websocket()

    with patch.object(handler, "_check_instance_lock_or_block", AsyncMock(return_value=False)), \
         patch.object(handler, "_check_paywall_or_block", AsyncMock(return_value=False)), \
         patch.object(handler, "_report_error_to_mothership", AsyncMock()):
        await handler.handle_request({"thread_id": "t", "message": "hi"}, websocket)

    # The report is fired as a background task; drain it.
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    credits = handler.app.state.paywall_credits
    assert credits["plan"] == "pro"
    assert credits["resets_at"] == "2026-09-06T00:00:00-04:00"
    assert credits["messages_remaining"] == 4
