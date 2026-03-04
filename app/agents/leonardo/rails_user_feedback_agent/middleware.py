"""
Middleware for Rails User Feedback Agent (User Mode v2).

Reuses middleware from rails_agent and adds user-mode-specific middleware.
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
# User Mode Context Injection (user-mode-specific)
# =============================================================================

class UserModeContextMiddleware(AgentMiddleware):
    """Inject user mode restrictions reminder into LLM context.

    This middleware adds a system note reminding the agent that it's in User Mode
    and can only READ code files and CREATE feedback entries. No code changes allowed.
    """

    def _inject_user_mode_context(self, request):
        """Add user mode context to the last user message."""
        context = '<CONTEXT type="mode">USER MODE: READ any file, QUERY database (SELECT only), CREATE feedback entries via write_feedback(). NO code changes, NO git operations, NO file writes, NO destructive DB commands.</CONTEXT>'

        messages = list(request.messages)
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                content = messages[i].content
                # Skip if already has user mode context
                if isinstance(content, str) and '<CONTEXT type="mode">' in content:
                    return request
                # Prepend context
                if isinstance(content, str):
                    messages[i] = HumanMessage(content=context + '\n\n' + content)
                    return request.override(messages=messages)
        return request

    def wrap_model_call(self, request, handler):
        """Sync version: Inject user mode context into LLM request."""
        modified_request = self._inject_user_mode_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject user mode context into LLM request."""
        modified_request = self._inject_user_mode_context(request)
        return await handler(modified_request)


# =============================================================================
# Convenience exports
# =============================================================================

# User-mode-specific middleware instance
inject_user_mode_context = UserModeContextMiddleware()

# Re-export from rails_agent for convenience
__all__ = [
    # From rails_agent
    'ViewPathContextMiddleware',
    'FailureCircuitBreakerMiddleware',
    'DynamicModelMiddleware',
    'inject_view_context',
    'check_failure_limit',
    # User-mode-specific
    'UserModeContextMiddleware',
    'inject_user_mode_context',
]
