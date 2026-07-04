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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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


class RefreshSkillCatalogMiddleware(AgentMiddleware):
    """Keep the ``use_skill`` tool's ``<available_skills>`` catalog in sync with the
    skills on disk, per request.

    Compiled agent graphs are cached at startup (see
    ``request_handler.get_app_from_workflow_string``), so the catalog baked into
    ``use_skill``'s description at build time would go stale the moment a skill is
    authored or deleted mid-session. This middleware rebuilds that one tool from
    the current ``.leonardo/skills/`` catalog before each model call and overrides
    the request's tool list — giving Claude-Code-style "newly authored skills
    appear immediately" discovery.

    It only overrides when the catalog actually changed (description differs), so
    a stable library keeps the tools payload byte-identical and prompt caching
    intact. Agents without a ``use_skill`` tool are untouched (no-op).
    """

    def _maybe_refresh(self, tools):
        # Lazy import avoids a circular import (tools.py imports this module's
        # sibling agents only indirectly; keep the dependency one-way).
        from app.agents.leonardo.rails_agent.tools import build_use_skill_tool

        for i, t in enumerate(tools):
            if getattr(t, "name", None) == "use_skill":
                fresh = build_use_skill_tool()
                if fresh.description != getattr(t, "description", None):
                    new_tools = list(tools)
                    new_tools[i] = fresh
                    return new_tools
                return None
        return None

    def wrap_model_call(self, request, handler):
        new_tools = self._maybe_refresh(request.tools)
        if new_tools is not None:
            return handler(request.override(tools=new_tools))
        return handler(request)

    async def awrap_model_call(self, request, handler):
        new_tools = self._maybe_refresh(request.tools)
        if new_tools is not None:
            return await handler(request.override(tools=new_tools))
        return await handler(request)


class BrandContextMiddleware(AgentMiddleware):
    """Inject the project's brand guide into every model call, live.

    Compiled agent graphs are cached at startup (see
    ``request_handler.get_app_from_workflow_string``), so anything baked into the
    system prompt at build time — like the brand guide — would go stale the
    moment a user edits it. This middleware reads ``.leonardo/BRAND.md`` fresh
    before each model call and prepends it to the most recent human message, so
    brand edits take effect on the very next turn with no restart.

    Progressive disclosure is handled upstream by
    ``project_context.build_brand_context``: a short guide is inlined whole; a
    long one is reduced to the compact palette plus a pointer to the
    ``brand-guidelines`` skill. If no brand guide exists, this is a no-op.

    Idempotent within a request (guards on the ``<CONTEXT type="brand">`` tag)
    and request-transient — the modified message is never written back to the
    checkpointer.
    """

    TAG = '<CONTEXT type="brand">'

    def _already_injected(self, content) -> bool:
        if isinstance(content, str):
            return self.TAG in content
        if isinstance(content, list):
            return any(
                isinstance(b, dict) and b.get("type") == "text" and self.TAG in b.get("text", "")
                for b in content
            )
        return False

    def _prepend(self, content, block: str):
        """Prepend ``block`` to string or multimodal-list message content."""
        if isinstance(content, str):
            return block + content
        if isinstance(content, list):
            out = []
            added = False
            for b in content:
                if not added and isinstance(b, dict) and b.get("type") == "text":
                    out.append({"type": "text", "text": block + b.get("text", "")})
                    added = True
                else:
                    out.append(b)
            if not added:
                out.insert(0, {"type": "text", "text": block})
            return out
        return content

    def _inject(self, request):
        # Lazy import keeps agent_factory free of a heavy import at module load
        # and avoids any import cycle with project_context.
        from app.agents.leonardo.project_context import build_brand_context

        body = build_brand_context()
        if not body:
            return request

        messages = list(request.messages)
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                content = messages[i].content
                if self._already_injected(content):
                    return request
                block = (
                    f"{self.TAG}\nProject brand guide — honor these colors, logos, and "
                    f"style rules in any visual, design, or theming work.\n\n{body}\n"
                    "</CONTEXT>\n\n"
                )
                messages[i] = HumanMessage(content=self._prepend(content, block))
                return request.override(messages=messages)
        return request

    def wrap_model_call(self, request, handler):
        return handler(self._inject(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._inject(request))


def build_leonardo_agent(*, middleware=None, **kwargs):
    """Wrap ``create_agent`` so EVERY Leonardo agent gets the cross-cutting
    middleware that must never be omitted.

    Two things are added (idempotent ``isinstance`` guards):

    - ``RepairOrphanedToolCallsMiddleware`` (SI#112) — PREPENDED so it runs FIRST:
      all subsequent middleware and the model must see valid history (before
      ``SummarizationMiddleware``'s token counting). ``rails_agent`` already
      lists it explicitly, so the guard leaves it as mw[0].
    - ``RefreshSkillCatalogMiddleware`` — keeps ``use_skill``'s skill catalog
      live despite startup graph caching. It only swaps a tool (never touches
      messages), so it is order-insensitive; we insert it right AFTER repair to
      preserve the repair-first invariant.
    - ``BrandContextMiddleware`` — injects the current brand guide per request
      (same startup-caching reason as the skill catalog). It touches only the
      latest human message and is idempotent, so ordering is not critical; we
      insert it after the skill refresh.
    """
    mw = list(middleware or [])
    if not any(isinstance(m, RepairOrphanedToolCallsMiddleware) for m in mw):
        mw.insert(0, RepairOrphanedToolCallsMiddleware())
    if not any(isinstance(m, RefreshSkillCatalogMiddleware) for m in mw):
        mw.insert(1, RefreshSkillCatalogMiddleware())
    if not any(isinstance(m, BrandContextMiddleware) for m in mw):
        mw.insert(2, BrandContextMiddleware())
    return create_agent(middleware=mw, **kwargs)
