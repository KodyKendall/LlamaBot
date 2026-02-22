"""
Middleware for Rails Agent using LangChain 1.0 middleware architecture.

This module contains:
- ViewPathContextMiddleware: Prepends page context to user messages
- FailureCircuitBreakerMiddleware: Circuit breaker for failed tool calls
- DynamicModelMiddleware: Runtime LLM model selection based on state
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_deepseek import ChatDeepSeek
from langchain_core.language_models import LanguageModelInput
from google.api_core.exceptions import ResourceExhausted
from langchain_core.messages import HumanMessage, AIMessage
from typing import Any
import json
import logging
import os

from app.agents.leonardo.rails_agent.state import RailsAgentState

logger = logging.getLogger(__name__)


# =============================================================================
# Custom ChatDeepSeek with reasoning_content support for multi-turn
# =============================================================================

class ChatDeepSeekWithReasoning(ChatDeepSeek):
    """Custom ChatDeepSeek that properly includes reasoning_content in API requests.

    DeepSeek's reasoner model requires reasoning_content to be present in assistant
    messages during multi-turn conversations with tool calls. The base ChatDeepSeek
    class stores reasoning_content in additional_kwargs but doesn't include it when
    sending messages back to the API.

    This subclass overrides _get_request_payload to:
    1. Extract reasoning_content from AIMessage.additional_kwargs BEFORE parent conversion
    2. Inject it back into the payload AFTER parent conversion
    3. Auto-add empty reasoning_content for reasoner models if missing

    See: https://api-docs.deepseek.com/guides/thinking_mode#tool-calls
    Fix based on: https://github.com/langchain-ai/langchain/pull/34516
    """

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        # Step 1: Extract reasoning_content from AIMessages BEFORE parent conversion
        # The parent method loses additional_kwargs, so we need to preserve them
        from langchain_core.messages import BaseMessage

        reasoning_contents = {}
        messages_list = input_ if isinstance(input_, list) else [input_]

        for i, msg in enumerate(messages_list):
            if isinstance(msg, AIMessage):
                # Extract reasoning_content from additional_kwargs
                reasoning = msg.additional_kwargs.get("reasoning_content")
                if reasoning is not None:
                    reasoning_contents[i] = reasoning
                else:
                    # For reasoner model, we need to add empty string if missing
                    reasoning_contents[i] = ""

        # Step 2: Get the base payload from parent
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # Step 3: Inject reasoning_content back into assistant messages
        # We need to match by index since role order may differ
        assistant_idx = 0
        for i, message in enumerate(payload["messages"]):
            if message["role"] == "assistant":
                # Find the corresponding original message index
                original_idx = None
                count = 0
                for j, msg in enumerate(messages_list):
                    if isinstance(msg, AIMessage):
                        if count == assistant_idx:
                            original_idx = j
                            break
                        count += 1

                if original_idx is not None and original_idx in reasoning_contents:
                    message["reasoning_content"] = reasoning_contents[original_idx]
                elif "reasoning_content" not in message:
                    # Fallback: add empty reasoning_content for reasoner model
                    message["reasoning_content"] = ""

                assistant_idx += 1

        return payload


# =============================================================================
# View Path Context Injection
# =============================================================================

class ViewPathContextMiddleware(AgentMiddleware):
    """Prepend page context to the last user message."""

    def _prepend_context_to_content(self, content, context: str):
        """Prepend context to message content, handling both string and multimodal list formats."""
        if isinstance(content, str):
            return context + content
        elif isinstance(content, list):
            # Multimodal content: find the first text block and prepend context to it
            new_content = []
            context_added = False
            for block in content:
                if not context_added and isinstance(block, dict) and block.get("type") == "text":
                    # Prepend context to the first text block
                    new_content.append({"type": "text", "text": context + block.get("text", "")})
                    context_added = True
                else:
                    new_content.append(block)
            # If no text block found, add context as a new text block at the start
            if not context_added:
                new_content.insert(0, {"type": "text", "text": context})
            return new_content
        else:
            # Unknown format, return as-is
            return content

    def _has_context_prefix(self, content) -> bool:
        """Check if content already has context prefix."""
        if isinstance(content, str):
            return content.startswith('<CONTEXT')
        elif isinstance(content, list):
            # Check first text block
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "").startswith('<CONTEXT')
            return False
        return False

    def wrap_model_call(self, request, handler):
        view_path = (request.state.get('debug_info') or {}).get('view_path')
        request_path = (request.state.get('debug_info') or {}).get('request_path')

        if view_path and request_path:
            messages = list(request.messages)
            # Find last HumanMessage
            for i in range(len(messages) - 1, -1, -1):
                if isinstance(messages[i], HumanMessage):
                    content = messages[i].content
                    # Skip if already has context
                    if self._has_context_prefix(content):
                        break
                    # Prepend context
                    context = f'<CONTEXT page="{request_path}" file="{view_path}"/>\n\n'
                    new_content = self._prepend_context_to_content(content, context)
                    messages[i] = HumanMessage(content=new_content)
                    return handler(request.override(messages=messages))

        return handler(request)

    async def awrap_model_call(self, request, handler):
        view_path = (request.state.get('debug_info') or {}).get('view_path')
        request_path = (request.state.get('debug_info') or {}).get('request_path')

        if view_path and request_path:
            messages = list(request.messages)
            for i in range(len(messages) - 1, -1, -1):
                if isinstance(messages[i], HumanMessage):
                    content = messages[i].content
                    if self._has_context_prefix(content):
                        break
                    context = f'<CONTEXT page="{request_path}" file="{view_path}"/>\n\n'
                    new_content = self._prepend_context_to_content(content, context)
                    messages[i] = HumanMessage(content=new_content)
                    return await handler(request.override(messages=messages))

        return await handler(request)


# =============================================================================
# Failure Circuit Breaker
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

    def _has_warning(self, content) -> bool:
        """Check if content already has failure warning."""
        warning_marker = '<CONTEXT type="warning">'
        if isinstance(content, str):
            return warning_marker in content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    if warning_marker in block.get("text", ""):
                        return True
            return False
        return False

    def _prepend_warning(self, content, warning: str):
        """Prepend warning to content, handling both string and multimodal list formats."""
        if isinstance(content, str):
            return warning + content
        elif isinstance(content, list):
            # Multimodal content: find the first text block and prepend warning to it
            new_content = []
            warning_added = False
            for block in content:
                if not warning_added and isinstance(block, dict) and block.get("type") == "text":
                    new_content.append({"type": "text", "text": warning + block.get("text", "")})
                    warning_added = True
                else:
                    new_content.append(block)
            # If no text block found, add warning as a new text block at the start
            if not warning_added:
                new_content.insert(0, {"type": "text", "text": warning})
            return new_content
        else:
            return content

    def _inject_failure_warning(self, request):
        """Add failure warning to the last user message if limit reached."""
        if not self._should_break(request.state):
            return request

        messages = list(request.messages)
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                content = messages[i].content
                # Skip if warning already injected
                if self._has_warning(content):
                    return request
                # Prepend warning
                warning = '<CONTEXT type="warning">Too many failed tool calls. DO NOT make any new tool calls. Tell the user it failed and ask them to try a different approach.</CONTEXT>\n\n'
                new_content = self._prepend_warning(content, warning)
                messages[i] = HumanMessage(content=new_content)
                return request.override(messages=messages)

        return request

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
# DeepSeek Reasoning Content Middleware
# =============================================================================

class DeepSeekReasoningMiddleware(AgentMiddleware):
    """Middleware to handle DeepSeek's reasoning_content requirement.

    DeepSeek's reasoner model requires that reasoning_content be present in
    AIMessages during multi-turn tool-calling conversations. This middleware
    ensures that AIMessages without reasoning_content get an empty string value,
    which satisfies the API requirement.

    See: https://api-docs.deepseek.com/guides/thinking_mode#tool-calls
    """

    def _inject_reasoning_content(self, messages, model_name: str):
        """Inject reasoning_content into AIMessages for DeepSeek reasoner."""
        if model_name != "deepseek-reasoner":
            return messages

        modified_messages = []
        for msg in messages:
            if isinstance(msg, AIMessage):
                # Check if reasoning_content is missing or None in additional_kwargs
                additional_kwargs = dict(msg.additional_kwargs) if msg.additional_kwargs else {}
                if "reasoning_content" not in additional_kwargs or additional_kwargs.get("reasoning_content") is None:
                    # Add empty reasoning_content to satisfy DeepSeek API
                    additional_kwargs["reasoning_content"] = ""
                    # Create new AIMessage with updated additional_kwargs
                    modified_messages.append(AIMessage(
                        content=msg.content,
                        additional_kwargs=additional_kwargs,
                        tool_calls=msg.tool_calls if hasattr(msg, 'tool_calls') else [],
                        id=msg.id if hasattr(msg, 'id') else None,
                    ))
                else:
                    modified_messages.append(msg)
            else:
                modified_messages.append(msg)

        return modified_messages

    def wrap_model_call(self, request, handler):
        """Sync version: Inject reasoning_content for DeepSeek."""
        model_name = request.state.get('llm_model', '')
        if model_name == "deepseek-reasoner":
            messages = self._inject_reasoning_content(list(request.messages), model_name)
            return handler(request.override(messages=messages))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject reasoning_content for DeepSeek."""
        model_name = request.state.get('llm_model', '')
        if model_name == "deepseek-reasoner":
            messages = self._inject_reasoning_content(list(request.messages), model_name)
            return await handler(request.override(messages=messages))
        return await handler(request)


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
        """Get LLM instance based on model name from frontend.

        All models are configured with thinking/reasoning enabled:
        - Gemini: include_thoughts=True, thinking_level="low"
        - Claude: thinking with budget_tokens (5000 for sonnet, 3000 for haiku)
        - OpenAI: reasoning with effort="low" and output_version for content blocks
        - DeepSeek: enable_reasoning=True via extra_body (OpenAI-compatible API)
        """
        # --- DeepSeek Integration ---
        # Using custom ChatDeepSeekWithReasoning for proper reasoning_content handling
        # in multi-turn tool-calling conversations
        if model_name == "deepseek-chat":
            # DeepSeek V3 non-thinking mode - good for general chat
            return ChatDeepSeek(
                model="deepseek-chat",
                max_tokens=8192,
                timeout=120,  # 2 minute timeout
            )
        elif model_name == "deepseek-reasoner":
            # DeepSeek V3 reasoning mode - shows chain-of-thought
            # ChatDeepSeekWithReasoning injects reasoning_content into API requests
            return ChatDeepSeekWithReasoning(
                model="deepseek-reasoner",
                max_tokens=8192,
                timeout=180,  # 3 minute timeout for reasoning model
            )
        elif model_name == "gpt-5-codex":
            return ChatOpenAI(
                model="gpt-5-codex",
                use_responses_api=True,
                reasoning={"effort": "low", "summary": "auto"},  # summary enables reasoning output
                output_version="responses/v1"  # Format reasoning into content blocks
            )
        elif model_name == "gpt-5-mini":
            return ChatOpenAI(
                model="gpt-5-mini",
                use_responses_api=True,
                reasoning={"effort": "low", "summary": "auto"},  # summary enables reasoning output
                output_version="responses/v1"  # Format reasoning into content blocks
            )
        elif model_name == "claude-4.5-sonnet":
            return ChatAnthropic(
                model="claude-sonnet-4-5-20250929",
                max_tokens=16384,
                thinking={"type": "enabled", "budget_tokens": 5000}
            )
        elif model_name == 'gemini-3-flash':
            # Note: thinking_level requires langchain-google-genai >= 4.0.0
            # If you get "Unknown field for ThinkingConfig: thinking_level", upgrade the package
            return ChatGoogleGenerativeAI(
                model="gemini-3-flash-preview",
                include_thoughts=True
                # thinking_level="low"
            )
        elif model_name == 'gemini-3-pro':
            return ChatGoogleGenerativeAI(
                model="gemini-3-pro-preview",
                include_thoughts=True
            )
        elif model_name == 'claude-4.5-haiku':
            return ChatAnthropic(
                model="claude-haiku-4-5",
                max_tokens=16384,
                thinking={"type": "enabled", "budget_tokens": 3000}
            )

        # Default to Gemini 3 Flash with thinking enabled
        return ChatGoogleGenerativeAI(
            model="gemini-3-flash-preview",
            include_thoughts=True
            # thinking_level="low"
        )

    def wrap_model_call(self, request, handler):
        """Sync version: Override the model in the request based on state."""
        # llm_model = request.state.get('llm_model', 'gemini-3-flash')
        llm_model = request.state.get('llm_model', 'claude-4.5-haiku')
        logger.info(f"Using LLM model: {llm_model}")
        model = self._get_llm(llm_model)
        # This is all you need for automatic retries on rate limits from Google
        model = model.with_retry(
            retry_if_exception_type=(ResourceExhausted,),
            stop_after_attempt=5,
            wait_exponential_jitter=True,
        )
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

# Middleware instances to use in nodes.py
inject_view_context = ViewPathContextMiddleware()
check_failure_limit = FailureCircuitBreakerMiddleware()
deepseek_reasoning_fix = DeepSeekReasoningMiddleware()
