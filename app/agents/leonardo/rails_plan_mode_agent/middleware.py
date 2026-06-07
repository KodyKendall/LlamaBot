"""
Middleware for Rails Plan Mode Agent.

Reuses middleware from rails_agent and adds plan-mode-specific middleware.
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
# Plan Mode Context Injection (plan-mode-specific)
# =============================================================================

class PlanModeContextMiddleware(AgentMiddleware):
    """Inject plan mode phase awareness into LLM context.

    This middleware adds a system note reminding the agent that it's in Plan Mode
    and should follow the 6-phase workflow.
    """

    def _inject_plan_mode_context(self, request):
        """Add plan mode context to the last user message."""
        context = '<CONTEXT type="mode">PLAN MODE: Follow the 6-phase workflow (Clarify → Research → Refine → Present Plan → Implement → Verify). Always know which phase you are in. Do NOT skip phases. Do NOT build before the user approves the plan.</CONTEXT>'

        messages = list(request.messages)
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                content = messages[i].content
                # Skip if already has plan mode context
                if isinstance(content, str) and '<CONTEXT type="mode">' in content:
                    return request
                # Prepend context
                if isinstance(content, str):
                    messages[i] = HumanMessage(content=context + '\n\n' + content)
                    return request.override(messages=messages)
        return request

    def wrap_model_call(self, request, handler):
        """Sync version: Inject plan mode context into LLM request."""
        modified_request = self._inject_plan_mode_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject plan mode context into LLM request."""
        modified_request = self._inject_plan_mode_context(request)
        return await handler(modified_request)


# =============================================================================
# Convenience exports
# =============================================================================

# Plan-mode-specific middleware instance
inject_plan_mode_context = PlanModeContextMiddleware()

# Re-export from rails_agent for convenience
__all__ = [
    # From rails_agent
    'ViewPathContextMiddleware',
    'FailureCircuitBreakerMiddleware',
    'DynamicModelMiddleware',
    'inject_view_context',
    'check_failure_limit',
    # Plan-mode-specific
    'PlanModeContextMiddleware',
    'inject_plan_mode_context',
]
