"""
Middleware for Rails Testing Agent.

Reuses middleware from rails_agent and adds testing-mode-specific middleware.
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
# Testing Mode Context Injection (testing-mode-specific)
# =============================================================================

class TestingModeContextMiddleware(AgentMiddleware):
    """Inject testing mode context reminder into LLM context.

    This middleware adds a system note reminding the agent that it's in Testing Mode
    and should focus on TDD bug reproduction: write failing tests first, verify bugs,
    then guide user to fix.
    """

    def _inject_testing_mode_context(self, request):
        """Add testing mode context to the last user message."""
        context = '<CONTEXT type="mode">TESTING MODE: Focus on TDD bug reproduction. (1) Capture bug report, (2) Write failing test proving bug exists, (3) Run test to verify RED state, (4) Hand off to Engineer mode for fix, (5) Verify test passes (GREEN) after fix. Tests become permanent regression guards.</CONTEXT>'

        messages = list(request.messages)
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                content = messages[i].content
                # Skip if already has testing mode context
                if isinstance(content, str) and '<CONTEXT type="mode">' in content:
                    return request
                # Prepend context
                if isinstance(content, str):
                    messages[i] = HumanMessage(content=context + '\n\n' + content)
                    return request.override(messages=messages)
        return request

    def wrap_model_call(self, request, handler):
        """Sync version: Inject testing mode context into LLM request."""
        modified_request = self._inject_testing_mode_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject testing mode context into LLM request."""
        modified_request = self._inject_testing_mode_context(request)
        return await handler(modified_request)


# =============================================================================
# Convenience exports
# =============================================================================

# Testing-mode-specific middleware instance
inject_testing_mode_context = TestingModeContextMiddleware()

# Re-export from rails_agent for convenience
__all__ = [
    # From rails_agent
    'ViewPathContextMiddleware',
    'FailureCircuitBreakerMiddleware',
    'DynamicModelMiddleware',
    'inject_view_context',
    'check_failure_limit',
    # Testing-mode-specific
    'TestingModeContextMiddleware',
    'inject_testing_mode_context',
]
