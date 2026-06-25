"""
Middleware for Rails Engineer Plan Mode Agent.

Reuses middleware from rails_agent and adds engineer-plan-mode-specific context
injection. Mirrors rails_plan_mode_agent/middleware.py.
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
# Engineer Plan Mode Context Injection
# =============================================================================

class EngineerPlanModeContextMiddleware(AgentMiddleware):
    """Inject engineer-plan-mode phase awareness into LLM context.

    Adds a system note reminding the agent that it's in Engineer Plan Mode and must
    follow the 6-phase workflow (plan before building) while retaining full engineering
    depth.
    """

    def _inject_plan_mode_context(self, request):
        """Add engineer-plan-mode context to the last user message."""
        context = '<CONTEXT type="mode">ENGINEER PLAN MODE: Follow the 6-phase workflow (Clarify → Research → Refine → Present Plan → Implement → Verify). Ask plain-language questions ONE at a time. Always know which phase you are in. Do NOT skip phases. Do NOT build before the user approves the plan. Build to full Engineer Mode quality once approved.</CONTEXT>'

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
        """Sync version: Inject engineer-plan-mode context into LLM request."""
        modified_request = self._inject_plan_mode_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject engineer-plan-mode context into LLM request."""
        modified_request = self._inject_plan_mode_context(request)
        return await handler(modified_request)


# =============================================================================
# Convenience exports
# =============================================================================

# Engineer-plan-mode-specific middleware instance
inject_engineer_plan_mode_context = EngineerPlanModeContextMiddleware()

# Re-export from rails_agent for convenience
__all__ = [
    # From rails_agent
    'ViewPathContextMiddleware',
    'FailureCircuitBreakerMiddleware',
    'DynamicModelMiddleware',
    'inject_view_context',
    'check_failure_limit',
    # Engineer-plan-mode-specific
    'EngineerPlanModeContextMiddleware',
    'inject_engineer_plan_mode_context',
]
