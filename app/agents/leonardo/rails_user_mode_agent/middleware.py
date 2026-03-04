"""
Middleware for Rails User Mode Agent (Database Mode).

Reuses middleware from rails_agent and adds database-mode-specific middleware.
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
# Database Mode Context Injection
# =============================================================================

class DatabaseModeContextMiddleware(AgentMiddleware):
    """Inject database mode context reminder into LLM context.

    This middleware adds a system note reminding the agent that it's in Database Mode
    and has full ActiveRecord access but cannot modify code files.
    """

    def _inject_database_mode_context(self, request):
        """Add database mode context to the last user message."""
        context = '<CONTEXT type="mode">DATABASE MODE: Full ActiveRecord access via bash_command (CREATE, READ, UPDATE, DELETE). NO code files, NO git, NO system commands. Confirm destructive operations with user first.</CONTEXT>'

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
        """Sync version: Inject database mode context into LLM request."""
        modified_request = self._inject_database_mode_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject database mode context into LLM request."""
        modified_request = self._inject_database_mode_context(request)
        return await handler(modified_request)


# =============================================================================
# Convenience exports
# =============================================================================

# Database-mode-specific middleware instance
inject_database_mode_context = DatabaseModeContextMiddleware()

# Re-export from rails_agent for convenience
__all__ = [
    # From rails_agent
    'ViewPathContextMiddleware',
    'FailureCircuitBreakerMiddleware',
    'DynamicModelMiddleware',
    'inject_view_context',
    'check_failure_limit',
    # Database-mode-specific
    'DatabaseModeContextMiddleware',
    'inject_database_mode_context',
]
