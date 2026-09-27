"""A blocked customer can always keep going on their own ChatGPT account (0.7.11).

Darren, 2026-09-23: "If people run out of tokens/messages, they should always be
able to use their own ChatGPT account and it should work." The gate ran before the
message's model was read, so on leo-lozeki switching to ChatGPT changed nothing.
"""
import contextvars
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketState

from app.agents.leonardo import customer_paid, model_policy
from app.websocket.request_handler import RequestHandler

BLOCKED = {"allowed_next": False, "messages_remaining": 0, "block_reason": "spend_limit"}


@pytest.fixture(autouse=True)
def _box(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_POLICY_PATH", str(tmp_path / "model_policy.json"))
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    for var in ("ENABLED_MODELS", "DISABLED_MODELS", "DEFAULT_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PAYWALL_ENABLED", "true")


def _ws():
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    ws.send_json = AsyncMock()
    return ws


def _handler(cached=BLOCKED):
    handler = RequestHandler(MagicMock())
    handler.app.state.paywall_credits = dict(cached)
    mothership = MagicMock()
    mothership.check_paywall = AsyncMock(return_value={"allowed": False, "messages_remaining": 0})
    handler.app.state.mothership_client = mothership
    return handler


async def _gate(handler, ws, llm_model):
    """Run the gate in its own context, like one background run; report what it left behind."""
    async def _run():
        blocked = await handler._paywall_blocks_turn({"llm_model": llm_model}, ws)
        return blocked, customer_paid.turn_is_customer_paid()

    import asyncio
    return await asyncio.create_task(_run(), context=contextvars.copy_context())


def _frames(ws):
    return [c.args[0].get("type") for c in ws.send_json.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-luna-chatgpt", "gpt-6-sol-chatgpt"])
async def test_blocked_customer_on_chatgpt_gets_through_on_their_own_plan(model):
    handler, ws = _handler(), _ws()

    blocked, customer_paid_turn = await _gate(handler, ws, model)

    assert blocked is False
    assert customer_paid_turn is True, "the turn must be pinned to the customer's plan"
    assert "paywall_hit" not in _frames(ws)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["deepseek-v4-flash", "muse-spark-1.2-contributor", None])
async def test_blocked_customer_on_a_platform_model_is_still_blocked(model):
    handler, ws = _handler(), _ws()

    blocked, customer_paid_turn = await _gate(handler, ws, model)

    assert blocked is True
    assert customer_paid_turn is False
    assert "paywall_hit" in _frames(ws)


@pytest.mark.asyncio
async def test_policy_disabled_chatgpt_model_is_not_a_way_around_the_paywall():
    from app.services import model_policy_store

    model_policy_store.save({"disabled_models": ["gpt-6-luna-chatgpt"]})
    handler, ws = _handler(), _ws()

    blocked, _ = await _gate(handler, ws, "gpt-6-luna-chatgpt")

    assert blocked is True


@pytest.mark.asyncio
async def test_unblocked_customer_is_unchanged():
    handler, ws = _handler(cached={"allowed_next": True}), _ws()

    blocked, customer_paid_turn = await _gate(handler, ws, "gpt-6-luna-chatgpt")

    assert blocked is False
    assert customer_paid_turn is False


@pytest.mark.asyncio
async def test_paywall_hit_says_whether_chatgpt_is_a_way_out():
    handler, ws = _handler(), _ws()
    await _gate(handler, ws, "deepseek-v4-flash")
    frame = ws.send_json.await_args_list[-1].args[0]
    assert frame["chatgpt_available"] is True

    from app.services import model_policy_store
    model_policy_store.save({"disabled_models": [
        "gpt-6-luna-chatgpt", "gpt-6-sol-chatgpt", "gpt-6-astra-chatgpt",
    ]})
    handler, ws = _handler(), _ws()
    await _gate(handler, ws, "deepseek-v4-flash")
    frame = ws.send_json.await_args_list[-1].args[0]
    assert frame["chatgpt_available"] is False
