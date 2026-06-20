"""
Middleware for Rails Ticket Mode Agent.

Reuses middleware from rails_agent and adds ticket-mode-specific middleware.
"""

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# Reuse middleware from rails_agent
from app.agents.leonardo.rails_agent.middleware import (
    ViewPathContextMiddleware,
    FailureCircuitBreakerMiddleware,
    DynamicModelMiddleware,
    inject_view_context,
    check_failure_limit,
)


# =============================================================================
# Ticket Mode Context Injection (ticket-mode-specific)
# =============================================================================

class TicketModeContextMiddleware(AgentMiddleware):
    """Inject ticket mode restrictions reminder into LLM context.

    This middleware adds a system note reminding the agent that it's in Ticket Mode
    and can only READ code files but WRITE only to .md files in rails/requirements/.
    """

    def _inject_ticket_mode_context(self, request):
        """Add ticket mode context to the last user message."""
        context = '<CONTEXT type="mode">TICKET MODE: READ any file, WRITE only .md files in rails/requirements/. No code changes. Focus on: observation template, research, implementation tickets.</CONTEXT>'

        messages = list(request.messages)
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                content = messages[i].content
                # Skip if already has ticket mode context
                if isinstance(content, str) and '<CONTEXT type="mode">' in content:
                    return request
                # Prepend context
                if isinstance(content, str):
                    messages[i] = HumanMessage(content=context + '\n\n' + content)
                    return request.override(messages=messages)
        return request

    def wrap_model_call(self, request, handler):
        """Sync version: Inject ticket mode context into LLM request."""
        modified_request = self._inject_ticket_mode_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject ticket mode context into LLM request."""
        modified_request = self._inject_ticket_mode_context(request)
        return await handler(modified_request)


# =============================================================================
# Deterministic Implementation Offer (ticket-mode-specific)
# =============================================================================

# Marker emitted by write_final_ticket on success (see nodes.py).
_TICKET_SUCCESS_MARKER = "Ticket created successfully with ID:"


class EnsureImplementationOfferMiddleware(AgentMiddleware):
    """Guarantee the "implement this ticket?" offer fires after a ticket is created.

    The intended flow is: write_final_ticket() succeeds -> the model calls
    offer_implementation(), whose interrupt() pauses the graph and surfaces the
    Yes/No buttons in the UI.

    In practice the default ticket-mode model (deepseek-v4-flash) only emits that
    offer_implementation tool call ~2/3 of the time. The rest of the time it ends
    its turn in prose ("Would you like me to switch to engineer mode?") with no
    tool call — so no interrupt fires and the user never sees the buttons. This
    was reported on Lohman (0.5.1c) as "the yes/no option doesn't consistently
    show up."

    This middleware removes the dependency on that second LLM decision. After the
    model runs, if a ticket was just created successfully and the turn ended
    WITHOUT an offer_implementation tool call, it injects the offer_implementation
    tool call itself. offer_implementation is replay-safe — it calls interrupt()
    before doing anything else and has no side effects — so forcing it produces
    exactly the happy-path behavior (interrupt -> buttons -> on resume the model
    sees the user's decision).
    """

    @staticmethod
    def _build_offer(messages):
        """Return injected-AIMessage update if an offer is owed, else None.

        An offer is owed when:
        - the last message is an AIMessage that ended the turn (no tool calls), and
        - the most recent write_final_ticket succeeded, and
        - no offer_implementation tool call has been made after that success.
        """
        if not messages:
            return None

        last = messages[-1]
        # Only act when the model handed control back without scheduling work.
        # If it scheduled any tool call (a real offer, or read_file/etc.), let it run.
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None

        # Map tool_call_id -> the write_final_ticket call args, so a success
        # ToolMessage can be tied back to the title/content it was created from.
        write_calls = {}
        for m in messages:
            if isinstance(m, AIMessage):
                for tc in (m.tool_calls or []):
                    if tc.get("name") == "write_final_ticket":
                        write_calls[tc.get("id")] = tc.get("args", {}) or {}

        if not write_calls:
            return None

        # Find the most recent SUCCESSFUL write_final_ticket and its position.
        success_idx = None
        success_args = None
        success_ticket_id = None
        for i, m in enumerate(messages):
            if (
                isinstance(m, ToolMessage)
                and m.tool_call_id in write_calls
                and _TICKET_SUCCESS_MARKER in (m.content or "")
            ):
                success_idx = i
                success_args = write_calls[m.tool_call_id]
                success_ticket_id = str(m.content).split(_TICKET_SUCCESS_MARKER)[1].strip().split()[0]

        if success_idx is None:
            return None

        # Idempotency: don't re-offer if an offer_implementation tool call already
        # happened after this successful write (model did it, or we already did).
        for m in messages[success_idx + 1:]:
            if isinstance(m, AIMessage):
                for tc in (m.tool_calls or []):
                    if tc.get("name") == "offer_implementation":
                        return None

        # Reconstruct the engineer-facing content the prompt specifies for
        # offer_implementation: description + research_notes + notes.
        parts = [
            success_args.get("description", ""),
            success_args.get("research_notes", ""),
            success_args.get("notes", ""),
        ]
        ticket_content = "\n\n".join(p for p in parts if p)
        ticket_title = success_args.get("title", "")

        logger.info(
            "EnsureImplementationOfferMiddleware: model ended turn without "
            "offer_implementation after creating ticket %s; injecting offer.",
            success_ticket_id,
        )
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "offer_implementation",
                        "args": {
                            "ticket_id": success_ticket_id,
                            "ticket_title": ticket_title,
                            "ticket_content": ticket_content,
                        },
                        "id": f"offer_auto_{success_ticket_id}",
                        "type": "tool_call",
                    }],
                )
            ]
        }

    def after_model(self, state, runtime):
        return self._build_offer(state["messages"])

    async def aafter_model(self, state, runtime):
        return self._build_offer(state["messages"])


# =============================================================================
# Convenience exports
# =============================================================================

# Ticket-mode-specific middleware instance
inject_ticket_mode_context = TicketModeContextMiddleware()

# Deterministic implementation-offer guarantee
ensure_implementation_offer = EnsureImplementationOfferMiddleware()

# Re-export from rails_agent for convenience
__all__ = [
    # From rails_agent
    'ViewPathContextMiddleware',
    'FailureCircuitBreakerMiddleware',
    'DynamicModelMiddleware',
    'inject_view_context',
    'check_failure_limit',
    # Ticket-mode-specific
    'TicketModeContextMiddleware',
    'inject_ticket_mode_context',
    'EnsureImplementationOfferMiddleware',
    'ensure_implementation_offer',
]
