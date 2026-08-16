"""
Middleware for Rails Agent using LangChain 1.0 middleware architecture.

This module contains:
- ViewPathContextMiddleware: Prepends page context to user messages
- FailureCircuitBreakerMiddleware: Circuit breaker for failed tool calls
- DynamicModelMiddleware: Runtime LLM model selection based on state
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from typing import Any
import asyncio
import logging
import time

from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.llm_factory import get_llm, system_message_for_model
# The box's resolved default (Muse where the box has a META key, DeepSeek
# where it does not) — never a hardcoded id, or a turn that arrives without
# an explicit llm_model silently ignores the fleet default.
from app.agents.leonardo.model_policy import enabled_default_model
from app.agents.leonardo.resilience import (
    is_transient_error,
    _MODEL_RETRY_MAX_ATTEMPTS,
    _MODEL_RETRY_BASE_DELAY,
    _MODEL_RETRY_MAX_DELAY,
    _model_retry_delay,
)
from app.agents.leonardo.model_capabilities import (
    get_model_capabilities,
    get_file_category,
)

logger = logging.getLogger(__name__)


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
# Strip Unsupported Multimodal Content
# =============================================================================

class StripUnsupportedMultimodalMiddleware(AgentMiddleware):
    """Remove multimodal blocks the active model can't consume from replayed history.

    A thread can be started on a vision model (e.g. Gemini), which persists
    ``image_url`` / ``file`` content blocks into the conversation history. If the
    user then switches to a text-only model (e.g. DeepSeek), that history is
    replayed and the provider rejects the request with a 400:

        Failed to deserialize the JSON body into the target type:
        messages[N]: unknown variant `image_url`, expected `text`

    Per-message attachment gating at *send* time (request_handler._build_message_content)
    can't fix this because the offending blocks are already in state. This
    middleware scrubs the whole message list right before the LLM call, keyed off
    the same MODEL_CAPABILITIES table, and replaces each dropped block with a short
    text note so the model knows something was attached rather than seeing a gap.
    """

    # content-block type -> capability key it requires
    _BLOCK_CAPABILITY = {
        "image_url": "images",
        "image": "images",
    }

    def _placeholder(self, capability: str, model_name: str) -> str:
        kind = {"images": "image", "video": "video", "pdf": "PDF"}.get(capability, "file")
        return (
            f"[A {kind} was attached here earlier, but it was removed because the "
            f"current model ({model_name}) can't see {kind}s.]"
        )

    def _block_capability(self, block: dict) -> str | None:
        """Return the capability a content block requires, or None if it's plain text/unknown."""
        btype = block.get("type")
        if btype in self._BLOCK_CAPABILITY:
            return self._BLOCK_CAPABILITY[btype]
        if btype == "file":
            # File blocks carry a mime_type; map it to images/video/pdf.
            mime = block.get("mime_type") or ""
            category = get_file_category(mime)
            return category if category != "unknown" else None
        return None

    def _strip_content(self, content, capabilities: dict, model_name: str):
        """Strip unsupported blocks from one message's content; collapse to str if only text remains."""
        if not isinstance(content, list):
            return content, False

        new_blocks = []
        changed = False
        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue
            capability = self._block_capability(block)
            if capability is not None and not capabilities.get(capability, False):
                new_blocks.append({
                    "type": "text",
                    "text": self._placeholder(capability, model_name),
                })
                changed = True
            else:
                new_blocks.append(block)

        if not changed:
            return content, False

        # Collapse to a plain string when nothing but text blocks survive — the
        # simplest valid shape for text-only providers.
        if all(isinstance(b, dict) and b.get("type") == "text" for b in new_blocks):
            return "\n\n".join(b.get("text", "") for b in new_blocks), True
        return new_blocks, True

    def _strip_unsupported(self, messages, model_name: str):
        """Return a message list with content the model can't consume removed."""
        capabilities = get_model_capabilities(model_name)
        # Fast path: model supports everything we ever attach.
        if capabilities.get("images") and capabilities.get("video") and capabilities.get("pdf"):
            return messages

        modified = []
        any_changed = False
        for msg in messages:
            new_content, changed = self._strip_content(msg.content, capabilities, model_name)
            if changed:
                any_changed = True
                modified.append(msg.model_copy(update={"content": new_content}))
            else:
                modified.append(msg)

        return modified if any_changed else messages

    def wrap_model_call(self, request, handler):
        model_name = request.state.get('llm_model') or enabled_default_model()
        messages = self._strip_unsupported(request.messages, model_name)
        if messages is not request.messages:
            return handler(request.override(messages=messages))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        model_name = request.state.get('llm_model') or enabled_default_model()
        messages = self._strip_unsupported(request.messages, model_name)
        if messages is not request.messages:
            return await handler(request.override(messages=messages))
        return await handler(request)


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
        if model_name != "deepseek-v4-flash":
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
        if model_name == "deepseek-v4-flash":
            messages = self._inject_reasoning_content(list(request.messages), model_name)
            return handler(request.override(messages=messages))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject reasoning_content for DeepSeek."""
        model_name = request.state.get('llm_model', '')
        if model_name == "deepseek-v4-flash":
            messages = self._inject_reasoning_content(list(request.messages), model_name)
            return await handler(request.override(messages=messages))
        return await handler(request)


# =============================================================================
# Dynamic Model Selection
# =============================================================================

# Rung 1 of the resilience ladder (docs/dev/error_telemetry.md): model-agnostic
# transient-error retry. We retry the model *call* (re-invoking the handler),
# NOT by wrapping the model in Runnable.with_retry — the latter returns a
# RunnableRetry that has no `bind_tools`, which breaks every tool-calling agent
# ('RunnableRetry' object has no attribute 'bind_tools'). Re-calling handler is
# the pattern langchain's own ModelRetryMiddleware uses, and it keeps the raw
# chat model intact. Deterministic errors (bad kwargs, 400s) are NOT retried —
# is_transient_error returns False — so they fall straight through to the
# fallback/floor rungs instead of failing identically N times.
#
# The retry constants + _model_retry_delay live in resilience.py (single source
# of truth); raw StateGraph nodes reuse them via invoke_with_transient_retry.
# They're imported into this module's namespace above, so references below (and
# tests reaching mw._MODEL_RETRY_MAX_ATTEMPTS) resolve unchanged.


class DynamicModelMiddleware(AgentMiddleware):
    """Middleware that dynamically switches LLM based on state.llm_model.

    Delegates model construction to app.agents.leonardo.llm_factory.get_llm so
    that all agents (middleware-based and direct-invoke) share one source of
    truth for the model registry.

    Note: Prompt caching for Anthropic is handled by passing a SystemMessage
    with cache_control to create_agent's system_prompt parameter in nodes.py.
    That SystemMessage carries LIST content (the cache blocks), which Fireworks
    and GMI reject outright — so the model swap here also normalizes the system
    message for the model it just selected (see system_message_for_model).
    """

    def _get_llm(self, model_name: str):
        """Backward-compatible alias for sub-agents that reach into the middleware."""
        return get_llm(model_name)

    def _override(self, request, llm_model):
        """Swap in the selected model AND a system message that model accepts."""
        overrides = {"model": get_llm(llm_model)}
        system_message = getattr(request, "system_message", None)
        if system_message is not None:
            overrides["system_message"] = system_message_for_model(system_message, llm_model)
        return request.override(**overrides)

    def wrap_model_call(self, request, handler):
        """Sync: select the model, then retry the call on transient failures."""
        llm_model = request.state.get('llm_model') or enabled_default_model()
        logger.info(f"Using LLM model: {llm_model}")
        req = self._override(request, llm_model)
        attempt = 0
        while True:
            try:
                return handler(req)
            except Exception as e:
                attempt += 1
                if attempt >= _MODEL_RETRY_MAX_ATTEMPTS or not is_transient_error(e):
                    raise
                logger.warning(
                    f"Transient model error on {llm_model} (attempt {attempt}/"
                    f"{_MODEL_RETRY_MAX_ATTEMPTS - 1}): {e!r}; retrying"
                )
                time.sleep(_model_retry_delay(attempt))

    async def awrap_model_call(self, request, handler):
        """Async: select the model, then retry the call on transient failures.

        The async path previously had NO retry at all — this closes that gap for
        the websocket chat path, which runs through here.
        """
        llm_model = request.state.get('llm_model') or enabled_default_model()
        logger.info(f"Using LLM model: {llm_model}")
        req = self._override(request, llm_model)
        attempt = 0
        while True:
            try:
                return await handler(req)
            except Exception as e:
                attempt += 1
                if attempt >= _MODEL_RETRY_MAX_ATTEMPTS or not is_transient_error(e):
                    raise
                logger.warning(
                    f"Transient model error on {llm_model} (attempt {attempt}/"
                    f"{_MODEL_RETRY_MAX_ATTEMPTS - 1}): {e!r}; retrying"
                )
                await asyncio.sleep(_model_retry_delay(attempt))


# =============================================================================
# Tool Result Image Clearing
# =============================================================================

class ToolResultImageClearingMiddleware(AgentMiddleware):
    """Strip images from old browser_inspect tool results to prevent summarization loops.

    Each browser_inspect call stores a base64 PNG screenshot (~25k-50k tokens) as an
    image_url block inside a ToolMessage. These accumulate in state and keep the
    conversation above the SummarizationMiddleware threshold on every turn, triggering
    an infinite summarize-on-every-turn loop.

    This middleware's before_model hook permanently strips image_url blocks from
    ToolMessages older than `keep_recent`, replacing them with a short placeholder so
    the model still knows the call was made. The most recent `keep_recent` screenshots
    are kept so the model can see the current page state.

    Place this FIRST in the middleware list so state is cleaned before
    SummarizationMiddleware counts tokens.
    """

    def __init__(self, keep_recent: int = None):
        from app.agents.utils.token_counter import SCREENSHOT_KEEP_RECENT
        super().__init__()
        self.keep_recent = keep_recent if keep_recent is not None else SCREENSHOT_KEEP_RECENT

    def _clear_old_images(self, messages):
        """Return (modified_messages, changed_flag).

        Strips image_url blocks from all but the last keep_recent ToolMessages
        that contain them. Returns (original_list, False) if nothing to clear.
        """
        from app.agents.utils.token_counter import _strip_old_images
        stripped = _strip_old_images(messages, keep_recent=self.keep_recent)
        return stripped, stripped is not messages

    def before_model(self, state, runtime):
        messages = state["messages"]
        modified, changed = self._clear_old_images(messages)
        if not changed:
            return None

        from langgraph.graph.message import REMOVE_ALL_MESSAGES
        from langchain_core.messages import RemoveMessage
        n_cleared = sum(
            1 for o, m in zip(messages, modified) if o is not m
        )
        logger.info(
            "ToolResultImageClearingMiddleware: cleared screenshots from %d message(s) "
            "(keeping last %d)", n_cleared, self.keep_recent
        )
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *modified,
            ]
        }

    async def abefore_model(self, state, runtime):
        return self.before_model(state, runtime)


# =============================================================================
# Orphaned Tool Call Repair Middleware
# =============================================================================

# RepairOrphanedToolCallsMiddleware (and its pure helper
# repair_orphaned_tool_calls_in_messages) now live in the shared
# app.agents.leonardo.agent_factory module so EVERY Leonardo graph can share one
# implementation — not just rails_agent (SupportIncident #112). Re-exported here
# for back-compat with existing imports.
from app.agents.leonardo.agent_factory import (  # noqa: E402
    RepairOrphanedToolCallsMiddleware,
    repair_orphaned_tool_calls_in_messages,
)


# =============================================================================
# Convenience exports (instantiated middleware)
# =============================================================================

# Middleware instances to use in nodes.py
inject_view_context = ViewPathContextMiddleware()
check_failure_limit = FailureCircuitBreakerMiddleware()
deepseek_reasoning_fix = DeepSeekReasoningMiddleware()
strip_unsupported_multimodal = StripUnsupportedMultimodalMiddleware()
clear_old_tool_images = ToolResultImageClearingMiddleware()
repair_orphaned_tool_calls = RepairOrphanedToolCallsMiddleware()
