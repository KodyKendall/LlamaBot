"""
Tests for EnsureImplementationOfferMiddleware.

Root problem (reproduced against deepseek-v4-flash): after write_final_ticket
succeeds, the model only emits the offer_implementation tool call ~2/3 of the
time. The other ~1/3 it ends the turn in prose ("Would you like me to switch to
engineer mode?") with NO tool call — so no interrupt fires and the user never
sees the Yes/No buttons.

This middleware makes the offer deterministic: when a ticket was just created
successfully and the turn ended WITHOUT an offer_implementation tool call, it
injects the offer_implementation tool call so the (replay-safe) interrupt fires
exactly as it would on the happy path.

We assert STRUCTURE (a tool call is/ isn't injected, with the right args), never
LLM text.
"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _mw():
    from app.agents.leonardo.rails_ticket_mode_agent.middleware import (
        ensure_implementation_offer,
    )
    return ensure_implementation_offer


def _write_call(call_id="w1", title="2026-06-17 - BUG: Rate Shows 0"):
    return AIMessage(
        content="Creating the ticket now.",
        tool_calls=[{
            "name": "write_final_ticket",
            "args": {
                "title": title,
                "description": "Rate shows 0 on the estimate page.",
                "ticket_type": "bug_debug",
                "research_notes": "Root cause: rate not persisted.",
                "notes": "Add a model-level round-trip test.",
            },
            "id": call_id,
            "type": "tool_call",
        }],
    )


def _write_success(call_id="w1", ticket_id="42"):
    return ToolMessage(
        content=f"Ticket created successfully with ID: {ticket_id}",
        tool_call_id=call_id,
    )


def _state(messages):
    return {"messages": messages}


def _injected_offer(update):
    """Return the injected offer_implementation tool call dict, or None."""
    if not update or "messages" not in update:
        return None
    for m in update["messages"]:
        for tc in (getattr(m, "tool_calls", None) or []):
            if tc.get("name") == "offer_implementation":
                return tc
    return None


class TestInjectsOfferWhenModelForgot:
    def test_injects_offer_after_successful_ticket_with_prose_ending(self):
        """The failing-in-prod case: ticket created, model ends turn in prose."""
        mw = _mw()
        messages = [
            HumanMessage(content="File a bug: rate shows 0."),
            _write_call(),
            _write_success(ticket_id="42"),
            AIMessage(content="Ticket created with ID: 42. Would you like me to "
                              "switch to engineer mode and implement this now?"),
        ]
        update = mw.after_model(_state(messages), None)
        offer = _injected_offer(update)
        assert offer is not None, "middleware should inject offer_implementation"
        assert offer["args"]["ticket_id"] == "42"
        assert offer["args"]["ticket_title"] == "2026-06-17 - BUG: Rate Shows 0"
        # ticket_content should carry the substance for the engineer agent
        content = offer["args"]["ticket_content"]
        assert "Rate shows 0 on the estimate page." in content
        assert "Root cause: rate not persisted." in content


class TestDoesNotDoubleOffer:
    def test_no_injection_when_model_already_called_offer(self):
        """Happy path: model emitted the offer tool call itself -> don't duplicate."""
        mw = _mw()
        messages = [
            HumanMessage(content="File a bug."),
            _write_call(),
            _write_success(),
            AIMessage(content="", tool_calls=[{
                "name": "offer_implementation",
                "args": {"ticket_id": "42", "ticket_title": "t", "ticket_content": "c"},
                "id": "o1", "type": "tool_call",
            }]),
        ]
        assert mw.after_model(_state(messages), None) is None

    def test_no_injection_when_offer_already_resolved(self):
        """Idempotency: an offer already ran (ToolMessage present) -> don't re-offer."""
        mw = _mw()
        messages = [
            HumanMessage(content="File a bug."),
            _write_call(),
            _write_success(),
            AIMessage(content="", tool_calls=[{
                "name": "offer_implementation",
                "args": {"ticket_id": "42", "ticket_title": "t", "ticket_content": "c"},
                "id": "o1", "type": "tool_call",
            }]),
            ToolMessage(content="User responded to implementation offer: no",
                        tool_call_id="o1"),
            AIMessage(content="Got it — ticket stays in backlog."),
        ]
        assert mw.after_model(_state(messages), None) is None


class TestNoSpuriousOffers:
    def test_no_injection_when_no_ticket_created(self):
        mw = _mw()
        messages = [
            HumanMessage(content="What files are in app/models?"),
            AIMessage(content="Here are the models you have..."),
        ]
        assert mw.after_model(_state(messages), None) is None

    def test_no_injection_when_ticket_write_failed(self):
        """write_final_ticket failed -> markdown fallback path, never offer."""
        mw = _mw()
        messages = [
            HumanMessage(content="File a bug."),
            _write_call(),
            ToolMessage(content="Failed to create ticket: connection refused",
                        tool_call_id="w1"),
            AIMessage(content="The DB write failed; I saved the ticket as markdown."),
        ]
        assert mw.after_model(_state(messages), None) is None

    def test_no_injection_while_model_still_working(self):
        """Turn ended with a (non-offer) tool call still pending -> let it run."""
        mw = _mw()
        messages = [
            HumanMessage(content="File a bug."),
            _write_call(),
            _write_success(),
            AIMessage(content="", tool_calls=[{
                "name": "read_file", "args": {"path": "x"}, "id": "r1",
                "type": "tool_call",
            }]),
        ]
        assert mw.after_model(_state(messages), None) is None
