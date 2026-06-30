"""
Shared agent construction helpers for all Leonardo agent graphs.

This module homes the cross-cutting machinery that must apply to EVERY Leonardo
agent — regardless of which mode (engineer, plan, beginner, ticket, user, …) the
user is in — so a fix made once can never be silently omitted from one graph.

Currently that means orphaned-tool-call repair (SupportIncident #112):

- ``repair_orphaned_tool_calls_in_messages`` — the pure, importable repair
  function. Used directly by the two raw-``StateGraph`` agents
  (``rails_beginner_agent``, ``rails_ai_builder_agent``) whose custom nodes call
  ``llm_with_tools.invoke(messages)`` and never run ``AgentMiddleware``.
- ``RepairOrphanedToolCallsMiddleware`` — the ``AgentMiddleware`` wrapper around
  that function, for the ``create_agent``-based agents.
- ``build_leonardo_agent`` — a ``create_agent`` wrapper that PREPENDS the repair
  middleware to every agent's stack, so no ``create_agent`` agent can forget it.

Background (SI#112): ``RepairOrphanedToolCallsMiddleware`` was originally defined
in ``rails_agent/middleware.py`` and wired into ``rails_agent`` ONLY. Every other
agent graph was unprotected, so an interrupted/crashed tool call (e.g. answering
an ``ask_user_question`` interrupt with plain chat) left an ``AIMessage`` with
``tool_calls`` and no matching ``ToolMessage`` — and every later turn hit a hard
``400 insufficient tool messages`` from the provider. Homing the logic here (the
same precedent as ``summarization.make_summarization_middleware``) lets all 11
graphs share one implementation with no awkward shared→rails_agent dependency.
"""

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
import logging

logger = logging.getLogger(__name__)


def repair_orphaned_tool_calls_in_messages(messages: list) -> list:
    """Return ``messages`` with a synthetic placeholder ``ToolMessage`` injected
    after any ``AIMessage`` tool_call that has no matching ``ToolMessage``.

    When a tool raises an unhandled exception (e.g. ``tavily.InvalidAPIKeyError``)
    or a HITL/interrupt tool call is left unresolved, the conversation state ends
    up with an ``AIMessage`` carrying ``tool_calls`` but no matching
    ``ToolMessage``s, permanently breaking all future turns in that thread (the
    provider rejects the history with ``400 insufficient tool messages``).

    This scans the whole thread and injects placeholders so the history is valid
    before the model sees it. Idempotent: returns the SAME list object when
    nothing needed repair (so callers can cheaply detect "no change").
    """
    # Collect all tool_call_ids that already have a ToolMessage in the thread.
    responded_ids = {
        msg.tool_call_id
        for msg in messages
        if isinstance(msg, ToolMessage) and msg.tool_call_id
    }

    result = []
    any_injected = False
    for msg in messages:
        result.append(msg)
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            missing = [
                tc for tc in msg.tool_calls
                if tc.get("id") and tc["id"] not in responded_ids
            ]
            for tc in missing:
                logger.warning(
                    "repair_orphaned_tool_calls: injecting placeholder "
                    "ToolMessage for orphaned tool_call_id=%s name=%s",
                    tc["id"], tc.get("name", "?"),
                )
                result.append(ToolMessage(
                    content=(
                        "Tool call did not complete — the tool may have crashed "
                        "(e.g. missing API key) or been interrupted. The operator "
                        "should check the server logs for the root cause."
                    ),
                    tool_call_id=tc["id"],
                ))
                responded_ids.add(tc["id"])
                any_injected = True

    return result if any_injected else messages


class RepairOrphanedToolCallsMiddleware(AgentMiddleware):
    """Inject placeholder ToolMessages for AIMessage tool_calls that have no response.

    Thin ``AgentMiddleware`` wrapper around
    :func:`repair_orphaned_tool_calls_in_messages`. The placeholders are NOT
    persisted to state — they exist only for the duration of the LLM call,
    keeping the fix invisible to the checkpointer.
    """

    def _repair(self, messages):
        return repair_orphaned_tool_calls_in_messages(messages)

    def wrap_model_call(self, request, handler):
        messages = self._repair(list(request.messages))
        if messages is not request.messages:
            return handler(request.override(messages=messages))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        messages = self._repair(list(request.messages))
        if messages is not request.messages:
            return await handler(request.override(messages=messages))
        return await handler(request)


def build_leonardo_agent(*, middleware=None, **kwargs):
    """Wrap ``create_agent`` so EVERY Leonardo agent gets the cross-cutting repair
    middleware that must never be omitted (SI#112).

    The repair middleware is PREPENDED, not appended: ``rails_agent`` deliberately
    lists repair FIRST ("Must be first so all subsequent middleware and the model
    see valid history") — it has to run before ``SummarizationMiddleware``'s token
    counting. The ``isinstance`` guard keeps this idempotent, so an agent that
    already lists the repair middleware explicitly (e.g. ``rails_agent``) is
    unchanged.
    """
    mw = list(middleware or [])
    if not any(isinstance(m, RepairOrphanedToolCallsMiddleware) for m in mw):
        mw.insert(0, RepairOrphanedToolCallsMiddleware())
    return create_agent(middleware=mw, **kwargs)
