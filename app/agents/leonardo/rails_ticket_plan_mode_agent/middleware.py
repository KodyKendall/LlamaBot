"""
Middleware for Rails Ticket Plan Mode Agent.

Reuses Ticket Mode's middleware (view context, dynamic model, the deterministic
implementation-offer guarantee, and the failure circuit breaker) and swaps in a single
combined mode-context injector that reminds the agent of BOTH the ticket-mode write
restrictions AND the plan-first "ground the story with questions" step.

A single combined injector is deliberate: the context injectors bail out if a
`<CONTEXT type="mode">` block already exists on the message, so stacking Ticket Mode's
injector and a separate plan-mode injector would let only one of them fire.
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

# Reuse the rest of Ticket Mode's middleware verbatim (DRY).
from app.agents.leonardo.rails_ticket_mode_agent.middleware import (
    ViewPathContextMiddleware,
    FailureCircuitBreakerMiddleware,
    DynamicModelMiddleware,
    EnsureImplementationOfferMiddleware,
    inject_view_context,
    check_failure_limit,
    ensure_implementation_offer,
)


# =============================================================================
# Ticket Plan Mode Context Injection (combined ticket restrictions + plan-first)
# =============================================================================

class TicketPlanModeContextMiddleware(AgentMiddleware):
    """Inject ticket-mode restrictions AND the plan-first grounding reminder.

    One combined `<CONTEXT type="mode">` block: keeps the Ticket Mode write rules and
    adds the Ticket Plan Mode requirement to ask clarifying questions (2-4 at a time,
    visual options for look-and-feel decisions) before drafting the observation, then
    proceed with the normal research -> ticket -> offer-to-implement flow.
    """

    def _inject_ticket_plan_mode_context(self, request):
        context = (
            '<CONTEXT type="mode">TICKET PLAN MODE: READ any file, WRITE only .md files in '
            'rails/requirements/. No code changes. FIRST ground the story — call '
            'ask_user_question with a `questions` list of 2-4 at once (set ui_related=true on any '
            'look-and-feel question; follow up with ask_user_uiux_question for visual '
            'options) before drafting the observation. THEN proceed as Ticket Mode: '
            'delegated research -> write_final_ticket -> offer_implementation.</CONTEXT>'
        )

        messages = list(request.messages)
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                content = messages[i].content
                # Skip if already has mode context
                if isinstance(content, str) and '<CONTEXT type="mode">' in content:
                    return request
                # Prepend context
                if isinstance(content, str):
                    messages[i] = HumanMessage(content=context + '\n\n' + content)
                    return request.override(messages=messages)
        return request

    def wrap_model_call(self, request, handler):
        """Sync version: Inject ticket-plan-mode context into LLM request."""
        modified_request = self._inject_ticket_plan_mode_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject ticket-plan-mode context into LLM request."""
        modified_request = self._inject_ticket_plan_mode_context(request)
        return await handler(modified_request)


# =============================================================================
# Convenience exports
# =============================================================================

# Ticket-plan-mode-specific combined context injector
inject_ticket_plan_mode_context = TicketPlanModeContextMiddleware()

__all__ = [
    # Reused from rails_ticket_mode_agent / rails_agent
    'ViewPathContextMiddleware',
    'FailureCircuitBreakerMiddleware',
    'DynamicModelMiddleware',
    'EnsureImplementationOfferMiddleware',
    'inject_view_context',
    'check_failure_limit',
    'ensure_implementation_offer',
    # Ticket-plan-mode-specific
    'TicketPlanModeContextMiddleware',
    'inject_ticket_plan_mode_context',
]
