"""
Middleware for Rails Ticket Mode Agent.

Reuses middleware from rails_agent and adds ticket-mode-specific middleware.
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

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
        """Add ticket mode context to messages."""
        context_msg = HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user is in Ticket Mode. You can READ any file but can ONLY WRITE/EDIT .md files in rails/requirements/. NO CODE CHANGES ALLOWED. Focus on: 1) Gathering complete observation template, 2) Technical research, 3) Creating implementation tickets. </NOTE_FROM_SYSTEM>"
        )

        new_messages = list(request.messages) + [context_msg]
        return request.override(messages=new_messages)

    def wrap_model_call(self, request, handler):
        """Sync version: Inject ticket mode context into LLM request."""
        modified_request = self._inject_ticket_mode_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject ticket mode context into LLM request."""
        modified_request = self._inject_ticket_mode_context(request)
        return await handler(modified_request)


# =============================================================================
# Convenience exports
# =============================================================================

# Ticket-mode-specific middleware instance
inject_ticket_mode_context = TicketModeContextMiddleware()

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
]
