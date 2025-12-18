"""
Middleware for Rails Agent using LangChain 1.0 middleware architecture.

This module contains:
- ViewPathContextMiddleware: Adds current page context to LLM messages (not persisted)
- FailureCircuitBreakerMiddleware: Circuit breaker for failed tool calls
- DynamicModelMiddleware: Runtime LLM model selection based on state
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage
from typing import Any
import logging

from app.agents.leonardo.rails_agent.state import RailsAgentState

logger = logging.getLogger(__name__)


# =============================================================================
# View Path Context Injection (via wrap_model_call - NOT persisted to state)
# =============================================================================

class ViewPathContextMiddleware(AgentMiddleware):
    """Inject current page view path into LLM context without persisting to state.

    This middleware adds context about which page the user is viewing, but only
    to the messages sent to the LLM - it does NOT persist to the conversation
    history that's shown in the UI.
    """

    def _inject_view_context(self, request):
        """Add view path context to messages if available."""
        view_path = (request.state.get('debug_info') or {}).get('view_path')
        request_path = (request.state.get('debug_info') or {}).get('request_path')
        if not view_path:
            return request

        # Create context message
        context_msg = HumanMessage(
            content=f"<NOTE_FROM_SYSTEM> The user is currently viewing their Ruby on Rails browser page at: {request_path} which resolves to the Rails file path at: {view_path} </NOTE_FROM_SYSTEM>"
        )

        # Append to messages for LLM only (not persisted)
        new_messages = list(request.messages) + [context_msg]
        return request.override(messages=new_messages)

    def wrap_model_call(self, request, handler):
        """Sync version: Inject view context into LLM request."""
        modified_request = self._inject_view_context(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject view context into LLM request."""
        modified_request = self._inject_view_context(request)
        return await handler(modified_request)


# =============================================================================
# Failure Circuit Breaker (via wrap_model_call for message injection)
# =============================================================================

class FailureCircuitBreakerMiddleware(AgentMiddleware):
    """Stop tool calls after 3 failures and instruct agent to ask user for help.

    This prevents infinite loops when tools consistently fail. After 3 failures,
    the agent is told to stop making tool calls and ask the user to try a different approach.

    The warning message is injected into the LLM request (not persisted to UI),
    and the failure counter is reset via before_model state update.
    """

    def _should_break(self, state) -> bool:
        """Check if we've hit the failure limit."""
        failed_count = state.get("failed_tool_calls_count", 0)
        return failed_count >= 3

    def _inject_failure_warning(self, request):
        """Add failure warning to messages if limit reached."""
        if not self._should_break(request.state):
            return request

        warning_msg = HumanMessage(
            content="<NOTE_FROM_SYSTEM> The user has had too many failed tool calls. DO NOT DO ANY NEW TOOL CALLS. Tell the user it's failed, and you need to stop and ask the user to try again in a different way. </NOTE_FROM_SYSTEM>"
        )

        new_messages = list(request.messages) + [warning_msg]
        return request.override(messages=new_messages)

    def before_model(self, state: RailsAgentState, runtime) -> dict[str, Any] | None:
        """Reset failure counter when limit is reached (this DOES persist to state)."""
        failed_count = state.get("failed_tool_calls_count", 0)
        if failed_count >= 3:
            # Reset counter by adding negative (since reducer uses operator.add)
            return {"failed_tool_calls_count": -failed_count}
        return None

    def wrap_model_call(self, request, handler):
        """Sync version: Inject failure warning into LLM request."""
        modified_request = self._inject_failure_warning(request)
        return handler(modified_request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject failure warning into LLM request."""
        modified_request = self._inject_failure_warning(request)
        return await handler(modified_request)


# =============================================================================
# Dynamic Model Selection
# =============================================================================

class DynamicModelMiddleware(AgentMiddleware):
    """Middleware that dynamically switches LLM based on state.llm_model.

    This allows the frontend to select which model to use for each request,
    supporting multiple providers (Anthropic, OpenAI).

    Note: Prompt caching for Anthropic is handled by passing a SystemMessage
    with cache_control to create_agent's system_prompt parameter in nodes.py.
    """

    def _get_llm(self, model_name: str):
        """Get LLM instance based on model name from frontend."""
        if model_name == "gpt-5-codex":
            return ChatOpenAI(
                model="gpt-5-codex",
                use_responses_api=True,
                reasoning={"effort": "low"}
            )
        elif model_name == "claude-4.5-sonnet":
            return ChatAnthropic(model="claude-sonnet-4-5-20250929", max_tokens=16384)
        elif model_name == 'gemini-3-flash':
            return ChatGoogleGenerativeAI(model="gemini-3-flash-preview")
        # Default to Claude 4.5 Haiku
        return ChatAnthropic(model="claude-haiku-4-5", max_tokens=16384)

    def wrap_model_call(self, request, handler):
        """Sync version: Override the model in the request based on state."""
        # llm_model = request.state.get('llm_model', 'gemini-3-flash')
        llm_model = request.state.get('llm_model', 'claude-4.5-haiku')
        logger.info(f"Using LLM model: {llm_model}")
        model = self._get_llm(llm_model)
        return handler(request.override(model=model))

    async def awrap_model_call(self, request, handler):
        """Async version: Override the model in the request based on state."""
        # llm_model = request.state.get('llm_model', 'gemini-3-flash')
        llm_model = request.state.get('llm_model', 'claude-4.5-haiku')
        logger.info(f"Using LLM model: {llm_model}")
        model = self._get_llm(llm_model)
        return await handler(request.override(model=model))


# =============================================================================
# Convenience exports (instantiated middleware)
# =============================================================================

# These are the middleware instances to use in nodes.py
inject_view_context = ViewPathContextMiddleware()
check_failure_limit = FailureCircuitBreakerMiddleware()
