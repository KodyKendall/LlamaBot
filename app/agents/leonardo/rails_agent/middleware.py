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
from app.agents.leonardo.llm_factory import (
    DEEPSEEK_DIRECT_MODELS,
    get_llm,
    system_message_for_model,
)
# The box's resolved default (Muse where the box has a META key, DeepSeek
# where it does not) — never a hardcoded id, or a turn that arrives without
# an explicit llm_model silently ignores the fleet default.
from app.agents.leonardo import model_health
from app.agents.leonardo.model_policy import enabled_default_model, fallback_model
from app.agents.leonardo.resilience import (
    is_midstream_stall,
    is_model_gone,
    is_transient_error,
    retry_notice_text,
    _MODEL_RETRY_MAX_ATTEMPTS,
    _MODEL_RETRY_BASE_DELAY,
    _MODEL_RETRY_MAX_DELAY,
    _MODEL_RETRY_MAX_TOTAL_SECONDS,
    _RETRY_NOTICE_AFTER_SECONDS,
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

    Scoped to DEEPSEEK_DIRECT_MODELS (api.deepseek.com), not every model with
    "deepseek" in the name: the same weights served by GMI and Fireworks accept
    assistant messages without reasoning_content, so this must not fire there.
    The membership test replaced a hardcoded ``== "deepseek-v4-flash"``, which
    silently skipped every other DeepSeek-direct model on the dropdown.

    See: https://api-docs.deepseek.com/guides/thinking_mode#tool-calls
    """

    def _inject_reasoning_content(self, messages, model_name: str):
        """Inject reasoning_content into AIMessages for DeepSeek reasoner."""
        if model_name not in DEEPSEEK_DIRECT_MODELS:
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
        if model_name in DEEPSEEK_DIRECT_MODELS:
            messages = self._inject_reasoning_content(list(request.messages), model_name)
            return handler(request.override(messages=messages))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        """Async version: Inject reasoning_content for DeepSeek."""
        model_name = request.state.get('llm_model', '')
        if model_name in DEEPSEEK_DIRECT_MODELS:
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


# How many times one step may move to a different model. One: escalation stays
# inside a single turn (docs/dev/error_telemetry.md §3, "the over-engineering
# trap"), and every extra rung is another full retry budget the user waits
# through. If the second model is down too, the box has a bigger problem than
# this ladder can paper over.
_MODEL_FALLBACK_MAX_RUNGS = 1

_IMAGE_BLOCK_TYPES = frozenset(
    {"image", "image_url", "input_image", "media", "video", "video_url"}
)


def _notify_retry(attempt: int, *, sync: bool):
    """Push "Model is not responding. Retrying (N of M)…" into the shimmer.

    Only called once the retry has cost the user real time — the threshold check
    lives at the call site so the fast-503 case does not even build a frame.
    Rides the same ``AIMessageChunk``-with-thinking shape ``/compact`` already
    uses, so it lands in the shimmer and not in the transcript as a message from
    Leo.

    Returns a coroutine to await on the async path, None on the sync path.
    """
    from app.lib import turn_notices

    frame = turn_notices.thinking_frame(retry_notice_text(attempt))
    if sync:
        try:
            turn_notices.notify(frame)
        except Exception as e:  # noqa: BLE001 - a notice never fails a turn
            logger.debug("could not announce a retry: %s", e)
        return None
    return turn_notices.anotify(frame)


def _request_carries_an_image(request) -> bool:
    """Whether this step is asking a model to look at something.

    Decides which fallback pool rung 2 may draw from, and the two ways of being
    wrong are NOT symmetric. Saying "image" about a text turn only narrows the
    fallback pool. Saying "text" about an image turn lets rung 2 pick a model
    that cannot see, which then either 400s on the image blocks or — worse —
    answers the question about the screenshot without the screenshot. So this
    reads every shape it can and treats any doubt as an image.

    Scope is the whole message list, not just the newest message, on purpose:
    the history travels with every step, so an image three turns back still has
    to be legible to whatever model finishes this one.
    """
    def _blocks(message):
        # Raw content AND LangChain's normalized view: the normalizer rewrites
        # `image_url` to `image`, and a provider-specific shape may only be
        # legible in one of the two. Checking both costs nothing and the cost of
        # a miss is a blind model answering a question about a screenshot.
        yield getattr(message, "content", None)
        try:
            yield message.content_blocks
        except Exception:  # noqa: BLE001 - older messages have no such view
            pass

    try:
        for message in getattr(request, "messages", None) or []:
            for content in _blocks(message):
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") in _IMAGE_BLOCK_TYPES:
                        return True
    except Exception as e:  # noqa: BLE001 - never break a turn deciding this
        logger.debug("could not inspect the request for images: %s", e)
    return False


def _announce_fallback(primary: str, fallback: str, *, sync: bool):
    """Say in the UI that the step finished somewhere else.

    Reuses ``model_substituted``, which is already built end to end (raised in
    request_handler, branched in MessageHandler.js, rendered as the banner above
    the composer). It only ever fired for *policy* substitution before the run
    started; ``reason`` is what lets the same banner word a mid-turn failure
    fallback differently.

    Returns the coroutine to await on the async path, or None on the sync path
    (where it has already been queued).
    """
    frame = {
        "type": "model_substituted",
        "requested": primary,
        "effective": fallback,
        "reason": "fallback",
    }
    from app.lib import turn_notices

    if sync:
        try:
            turn_notices.notify(frame)
        except Exception as e:  # noqa: BLE001 - a notice never fails a turn
            logger.debug("could not announce the model fallback: %s", e)
        return None
    return turn_notices.anotify(frame)


def _model_request_params(request):
    """Best-effort snapshot of the invocation parameters on the bound model.

    Reads whatever the provider client exposes rather than a fixed list — the next
    offending parameter is by definition one we are not thinking about today. Redaction
    happens in message_invariants.redact_request_params, not here.
    """
    model = getattr(request, "model", None)
    for attr in ("_default_params", "_identifying_params", "model_kwargs"):
        params = getattr(model, attr, None)
        if isinstance(params, dict) and params:
            return dict(params)
    return {}


def _record_bad_request_shape(exc, request, llm_model):
    """Attach a redacted description of a rejected request to the exception."""
    from app.agents.leonardo.message_invariants import (
        log_bad_request_shape,
        looks_like_bad_request,
    )

    if not looks_like_bad_request(exc):
        return
    try:
        log_bad_request_shape(
            exc,
            list(getattr(request, "messages", None) or []),
            tools=getattr(request, "tools", None),
            model=llm_model,
            label=f"model={llm_model}",
            # The parameters we actually sent. The provider answers "invalid parameters"
            # and then reports param=null on every occurrence, so this is the only way to
            # diff a failing box against a working one.
            request_params=_model_request_params(request),
        )
    except Exception:  # noqa: BLE001 - diagnosis must never mask the real error
        logger.debug("could not describe the rejected request", exc_info=True)


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

    def _next_rung(self, request, llm_model, tried):
        """Rung 2: the model to finish this step on, or None to give up.

        Resolved through model_policy — never a literal id here (see
        ``fallback_model``'s docstring and ``test_no_agent_hardcodes_a_fallback_
        model``). ``tried`` is what stops the two-dead-endpoint loop: on a Muse
        box the policy default IS the model that just stalled, so without it the
        resolver would hand Muse back as DeepSeek's fallback and the pair would
        bounce forever.
        """
        try:
            return fallback_model(
                llm_model,
                needs_vision=_request_carries_an_image(request),
                exclude=tried,
            )
        except Exception as e:  # noqa: BLE001 - no rung 2 beats no turn
            logger.warning("Could not resolve a fallback model: %s", e)
            return None

    def wrap_model_call(self, request, handler):
        """Sync: select the model, retry it, then fall back to another model.

        Two bounds, not one. The attempt counter alone is meaningless against a
        provider that stalls — see ``_MODEL_RETRY_MAX_TOTAL_SECONDS`` — so the
        wall clock ends rung 1 too, and rung 2 gets a turn instead of the user
        watching the same dead endpoint be retried five times.
        """
        llm_model = request.state.get('llm_model') or enabled_default_model()
        logger.info(f"Using LLM model: {llm_model}")
        req = self._override(request, llm_model)
        tried = {llm_model}
        fallbacks_used = 0
        attempt = 0
        started_at = time.monotonic()
        while True:
            try:
                return handler(req)
            except Exception as e:
                attempt += 1
                elapsed = time.monotonic() - started_at
                # A deterministic error (bad kwarg, 400) fails identically on
                # every provider, so it must not spend rung 2's budget either.
                # A RETIRED model is the one failure that is both pointless to
                # retry and perfectly recoverable by moving. It is not transient,
                # so before 0.7.6 it fell into the branch below and the raw
                # provider 404 went straight to the customer while every other
                # model sat there working — which is what turned Meta's
                # 2026-08-31 retirement of our default into a fleet outage.
                # Skip rung 1 entirely (a 404 is known-permanent on the first
                # response) and remember it so later turns do not pay for it.
                if is_model_gone(e):
                    model_health.mark_model_gone(llm_model)
                    fallback = (
                        None if fallbacks_used >= _MODEL_FALLBACK_MAX_RUNGS
                        else self._next_rung(request, llm_model, tried)
                    )
                    if fallback is None:
                        logger.error(
                            f"{llm_model} reports itself retired and there is no "
                            f"other model available on this box"
                        )
                        _record_bad_request_shape(e, req, llm_model)
                        raise
                    logger.warning(
                        f"{llm_model} reports itself retired; finishing this step "
                        f"on {fallback}"
                    )
                    _announce_fallback(llm_model, fallback, sync=True)
                    llm_model = fallback
                    tried.add(fallback)
                    fallbacks_used += 1
                    req = self._override(request, llm_model)
                    attempt = 0
                    started_at = time.monotonic()
                    continue
                # A stall AFTER content is a spent replica, not a flaky one.
                # StreamChunkTimeoutError is a TimeoutError, so it classifies as
                # transient and without this it takes the rung-1 branch below and
                # retries the endpoint that just threw away 478 chunks of real
                # work. On a model pinned to one provider with allow_fallbacks:
                # false that retry is guaranteed to land on the same sick replica,
                # and each attempt costs a whole generation rather than a fast
                # 503 — so rung 1's budget buys nothing before rung 2 finally
                # gets a turn. 38 dead turns across 6 customer boxes in the week
                # of 2026-08-31.
                #
                # Skip rung 1 the way a retirement does, but deliberately do NOT
                # mark_model_gone: the model is fine, this replica is not, and a
                # 15-minute fleet-wide ban on the box default over one bad stream
                # would be a self-inflicted outage. A zero-chunk stall keeps its
                # rung-1 retry — see is_midstream_stall.
                if is_midstream_stall(e):
                    stalled_after = getattr(e, "chunks_received", 0)
                    fallback = (
                        None if fallbacks_used >= _MODEL_FALLBACK_MAX_RUNGS
                        else self._next_rung(request, llm_model, tried)
                    )
                    if fallback is None:
                        logger.warning(
                            f"{llm_model} stalled after {stalled_after} chunks and "
                            f"there is no other model available on this box"
                        )
                        raise
                    logger.warning(
                        f"{llm_model} stalled after {stalled_after} chunks; "
                        f"finishing this step on {fallback}"
                    )
                    _announce_fallback(llm_model, fallback, sync=True)
                    llm_model = fallback
                    tried.add(fallback)
                    fallbacks_used += 1
                    req = self._override(request, llm_model)
                    attempt = 0
                    started_at = time.monotonic()
                    continue
                if not is_transient_error(e):
                    _record_bad_request_shape(e, req, llm_model)
                    raise
                if attempt >= _MODEL_RETRY_MAX_ATTEMPTS or elapsed >= _MODEL_RETRY_MAX_TOTAL_SECONDS:
                    fallback = (
                        None if fallbacks_used >= _MODEL_FALLBACK_MAX_RUNGS
                        else self._next_rung(request, llm_model, tried)
                    )
                    if fallback is None:
                        logger.warning(
                            f"Giving up on {llm_model} after {elapsed:.0f}s and "
                            f"{attempt} attempts; no fallback model available"
                        )
                        _record_bad_request_shape(e, req, llm_model)
                        raise
                    logger.warning(
                        f"{llm_model} stalled for {elapsed:.0f}s over {attempt} "
                        f"attempts; finishing this step on {fallback}"
                    )
                    _announce_fallback(llm_model, fallback, sync=True)
                    llm_model = fallback
                    tried.add(fallback)
                    fallbacks_used += 1
                    req = self._override(request, llm_model)
                    attempt = 0
                    started_at = time.monotonic()
                    # No backoff: the whole point of moving is that waiting on
                    # this provider has stopped being worth anything.
                    continue
                logger.warning(
                    f"Transient model error on {llm_model} (attempt {attempt}/"
                    f"{_MODEL_RETRY_MAX_ATTEMPTS - 1}): {e!r}; retrying"
                )
                if elapsed >= _RETRY_NOTICE_AFTER_SECONDS:
                    _notify_retry(attempt, sync=True)
                time.sleep(_model_retry_delay(attempt))

    async def awrap_model_call(self, request, handler):
        """Async: select the model, retry it, then fall back to another model.

        The async path previously had NO retry at all — this closes that gap for
        the websocket chat path, which runs through here. Same two bounds and
        same rung 2 as the sync loop above; this is the path a customer is
        actually on, so it is also the one that pushes the status frames.
        """
        llm_model = request.state.get('llm_model') or enabled_default_model()
        logger.info(f"Using LLM model: {llm_model}")
        req = self._override(request, llm_model)
        tried = {llm_model}
        fallbacks_used = 0
        attempt = 0
        started_at = time.monotonic()
        while True:
            try:
                return await handler(req)
            except Exception as e:
                attempt += 1
                elapsed = time.monotonic() - started_at
                # A RETIRED model is the one failure that is both pointless to
                # retry and perfectly recoverable by moving. It is not transient,
                # so before 0.7.6 it fell into the branch below and the raw
                # provider 404 went straight to the customer while every other
                # model sat there working — which is what turned Meta's
                # 2026-08-31 retirement of our default into a fleet outage.
                # Skip rung 1 entirely (a 404 is known-permanent on the first
                # response) and remember it so later turns do not pay for it.
                if is_model_gone(e):
                    model_health.mark_model_gone(llm_model)
                    fallback = (
                        None if fallbacks_used >= _MODEL_FALLBACK_MAX_RUNGS
                        else self._next_rung(request, llm_model, tried)
                    )
                    if fallback is None:
                        logger.error(
                            f"{llm_model} reports itself retired and there is no "
                            f"other model available on this box"
                        )
                        _record_bad_request_shape(e, req, llm_model)
                        raise
                    logger.warning(
                        f"{llm_model} reports itself retired; finishing this step "
                        f"on {fallback}"
                    )
                    await _announce_fallback(llm_model, fallback, sync=False)
                    llm_model = fallback
                    tried.add(fallback)
                    fallbacks_used += 1
                    req = self._override(request, llm_model)
                    attempt = 0
                    started_at = time.monotonic()
                    continue
                # A stall AFTER content is a spent replica, not a flaky one.
                # StreamChunkTimeoutError is a TimeoutError, so it classifies as
                # transient and without this it takes the rung-1 branch below and
                # retries the endpoint that just threw away 478 chunks of real
                # work. On a model pinned to one provider with allow_fallbacks:
                # false that retry is guaranteed to land on the same sick replica,
                # and each attempt costs a whole generation rather than a fast
                # 503 — so rung 1's budget buys nothing before rung 2 finally
                # gets a turn. 38 dead turns across 6 customer boxes in the week
                # of 2026-08-31.
                #
                # Skip rung 1 the way a retirement does, but deliberately do NOT
                # mark_model_gone: the model is fine, this replica is not, and a
                # 15-minute fleet-wide ban on the box default over one bad stream
                # would be a self-inflicted outage. A zero-chunk stall keeps its
                # rung-1 retry — see is_midstream_stall.
                if is_midstream_stall(e):
                    stalled_after = getattr(e, "chunks_received", 0)
                    fallback = (
                        None if fallbacks_used >= _MODEL_FALLBACK_MAX_RUNGS
                        else self._next_rung(request, llm_model, tried)
                    )
                    if fallback is None:
                        logger.warning(
                            f"{llm_model} stalled after {stalled_after} chunks and "
                            f"there is no other model available on this box"
                        )
                        raise
                    logger.warning(
                        f"{llm_model} stalled after {stalled_after} chunks; "
                        f"finishing this step on {fallback}"
                    )
                    await _announce_fallback(llm_model, fallback, sync=False)
                    llm_model = fallback
                    tried.add(fallback)
                    fallbacks_used += 1
                    req = self._override(request, llm_model)
                    attempt = 0
                    started_at = time.monotonic()
                    continue
                if not is_transient_error(e):
                    # A provider that rejects the REQUEST tells us nothing
                    # actionable ("'param': None" — 32 occurrences on 7 boxes in
                    # 7 days for our own default model). Describe the shape of
                    # what we sent, redacted, so the fleet report has something
                    # to diagnose from. It also fails identically on any other
                    # provider, so it must not reach rung 2.
                    _record_bad_request_shape(e, req, llm_model)
                    raise
                if attempt >= _MODEL_RETRY_MAX_ATTEMPTS or elapsed >= _MODEL_RETRY_MAX_TOTAL_SECONDS:
                    fallback = (
                        None if fallbacks_used >= _MODEL_FALLBACK_MAX_RUNGS
                        else self._next_rung(request, llm_model, tried)
                    )
                    if fallback is None:
                        logger.warning(
                            f"Giving up on {llm_model} after {elapsed:.0f}s and "
                            f"{attempt} attempts; no fallback model available"
                        )
                        _record_bad_request_shape(e, req, llm_model)
                        raise
                    logger.warning(
                        f"{llm_model} stalled for {elapsed:.0f}s over {attempt} "
                        f"attempts; finishing this step on {fallback}"
                    )
                    await _announce_fallback(llm_model, fallback, sync=False)
                    llm_model = fallback
                    tried.add(fallback)
                    fallbacks_used += 1
                    req = self._override(request, llm_model)
                    attempt = 0
                    started_at = time.monotonic()
                    continue
                logger.warning(
                    f"Transient model error on {llm_model} (attempt {attempt}/"
                    f"{_MODEL_RETRY_MAX_ATTEMPTS - 1}): {e!r}; retrying"
                )
                if elapsed >= _RETRY_NOTICE_AFTER_SECONDS:
                    await _notify_retry(attempt, sync=False)
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
