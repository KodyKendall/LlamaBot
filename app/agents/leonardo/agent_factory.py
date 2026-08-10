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
from app.agents.leonardo.turn_metrics_middleware import TurnMetricsMiddleware
import logging

logger = logging.getLogger(__name__)


def _raw_fallback_tool_calls(msg) -> list:
    """The ``additional_kwargs["tool_calls"]`` calls, normalized to the parsed shape.

    ``langchain_openai``'s serializer falls back to this raw list whenever BOTH
    parsed lists are empty, so these calls really do reach the provider. They are
    normalized to ``{"id", "name", "args"}`` here so every caller can read them
    exactly like a parsed call.
    """
    extra = getattr(msg, "additional_kwargs", None) or {}
    out = []
    for rc in extra.get("tool_calls") or []:
        if not isinstance(rc, dict):
            continue
        fn = rc.get("function") or {}
        out.append({
            "id": rc.get("id"),
            "name": fn.get("name") if isinstance(fn, dict) else None,
            "args": fn.get("arguments") if isinstance(fn, dict) else None,
        })
    return out


def emitted_tool_calls(msg) -> list:
    """Every tool call the serializer puts ON THE WIRE for an ``AIMessage``.

    ``langchain_openai`` emits ``tool_calls + invalid_tool_calls``, but every
    executor (``ToolNode``, ``create_agent``'s tool router) iterates
    ``tool_calls`` only. So a call whose ``arguments`` JSON did not parse — a
    response truncated at ``max_tokens`` mid-``write_file``, or DeepSeek emitting
    malformed args — is announced to the provider, never executed, and therefore
    never answered by a ``ToolMessage``. Repairs must scan what is emitted, not
    what is executable, or they see nothing wrong while the thread 400s forever.

    When BOTH parsed lists are empty the serializer falls back to the raw
    ``additional_kwargs["tool_calls"]``, so that list is what goes on the wire and
    this function must report it. Missing that fallback is how the 400 came BACK
    (fingerprint 4a3f1aa848a9): ``_drop_idless_tool_calls`` empties the parsed
    lists via ``model_copy``, which does not re-run ``AIMessage``'s validators, so
    an id-bearing raw call was left announced to the provider while every repair
    read the message as having no tool calls at all.
    """
    parsed = (
        list(getattr(msg, "tool_calls", None) or [])
        + list(getattr(msg, "invalid_tool_calls", None) or [])
    )
    return parsed if parsed else _raw_fallback_tool_calls(msg)


def _drop_idless_tool_calls(msg):
    """Return ``(msg, changed)`` with any id-less tool call removed.

    A tool call carrying a null/empty ``id`` is still serialized (as
    ``'id': None``) yet no ``ToolMessage`` can ever answer it — ``tool_call_id``
    must be a string. The only repair is to not send it at all, which costs
    nothing: with no id it was unroutable anyway.
    """
    tool_calls = list(getattr(msg, "tool_calls", None) or [])
    invalid_calls = list(getattr(msg, "invalid_tool_calls", None) or [])
    kept_calls = [tc for tc in tool_calls if tc.get("id")]
    kept_invalid = [tc for tc in invalid_calls if tc.get("id")]

    # The serializer falls back to `additional_kwargs["tool_calls"]` when both
    # parsed lists are empty, so the raw copy has to be filtered too or the
    # id-less call comes straight back on the wire. That fallback also means the
    # raw list must be checked even when the parsed lists needed no change — an
    # id-less raw call on an otherwise-empty message still reaches the provider.
    extra = getattr(msg, "additional_kwargs", None) or {}
    raw_calls = [rc for rc in (extra.get("tool_calls") or []) if isinstance(rc, dict)]
    kept_raw = [rc for rc in raw_calls if rc.get("id")]

    parsed_dropped = (
        (len(tool_calls) - len(kept_calls)) + (len(invalid_calls) - len(kept_invalid))
    )
    raw_dropped = len(raw_calls) - len(kept_raw)
    if not parsed_dropped and not raw_dropped:
        return msg, False

    logger.warning(
        "repair_orphaned_tool_calls: dropping %d tool call(s) with no id from "
        "AIMessage id=%s — they can never be answered",
        parsed_dropped or raw_dropped,
        getattr(msg, "id", "?"),
    )

    update = {"tool_calls": kept_calls, "invalid_tool_calls": kept_invalid}
    if extra.get("tool_calls"):
        new_extra = dict(extra)
        if kept_raw:
            new_extra["tool_calls"] = kept_raw
        else:
            new_extra.pop("tool_calls", None)
        update["additional_kwargs"] = new_extra

    return msg.model_copy(update=update), True


def repair_orphaned_tool_calls_in_messages(messages: list) -> list:
    """Return ``messages`` with a synthetic placeholder ``ToolMessage`` injected
    after any ``AIMessage`` tool_call that has no matching ``ToolMessage``.

    When a tool raises an unhandled exception (e.g. ``tavily.InvalidAPIKeyError``)
    or a HITL/interrupt tool call is left unresolved, the conversation state ends
    up with an ``AIMessage`` carrying ``tool_calls`` but no matching
    ``ToolMessage``s, permanently breaking all future turns in that thread (the
    provider rejects the history with ``400 insufficient tool messages``).

    ``invalid_tool_calls`` (malformed args JSON) are repaired the same way — see
    :func:`emitted_tool_calls` for why they are just as fatal — and an id-less
    call, which nothing can answer, is dropped from the outgoing message.

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
    any_repaired = False
    for msg in messages:
        if isinstance(msg, AIMessage) and emitted_tool_calls(msg):
            msg, dropped = _drop_idless_tool_calls(msg)
            any_repaired = any_repaired or dropped

        result.append(msg)
        if not isinstance(msg, AIMessage):
            continue

        missing = [
            tc for tc in emitted_tool_calls(msg)
            if tc.get("id") and tc["id"] not in responded_ids
        ]
        for tc in missing:
            is_invalid = bool(tc.get("error")) or tc.get("type") == "invalid_tool_call"
            logger.warning(
                "repair_orphaned_tool_calls: injecting placeholder ToolMessage "
                "for %s tool_call_id=%s name=%s",
                "malformed" if is_invalid else "orphaned",
                tc["id"], tc.get("name", "?"),
            )
            result.append(ToolMessage(
                content=(
                    "Tool call was not executed — its arguments were not valid "
                    "JSON (often a response cut off at the token limit mid-call). "
                    "Call the tool again with complete, valid arguments, splitting "
                    "the work into smaller calls if the payload was large."
                ) if is_invalid else (
                    "Tool call did not complete — the tool may have crashed "
                    "(e.g. missing API key) or been interrupted. The operator "
                    "should check the server logs for the root cause."
                ),
                tool_call_id=tc["id"],
            ))
            responded_ids.add(tc["id"])
            any_repaired = True

    return result if any_repaired else messages


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
    - ``TurnMetricsMiddleware`` — times every model and tool call. APPENDED so
      it sits OUTERMOST at the model-call boundary and therefore measures what
      the user actually waits for, including whatever the inner middleware
      (summarization, repair, brand injection) costs. Purely observational.
    """
    mw = list(middleware or [])
    if not any(isinstance(m, RepairOrphanedToolCallsMiddleware) for m in mw):
        mw.insert(0, RepairOrphanedToolCallsMiddleware())
    if not any(isinstance(m, RefreshSkillCatalogMiddleware) for m in mw):
        mw.insert(1, RefreshSkillCatalogMiddleware())
    if not any(isinstance(m, BrandContextMiddleware) for m in mw):
        mw.insert(2, BrandContextMiddleware())
    if not any(isinstance(m, TurnMetricsMiddleware) for m in mw):
        mw.append(TurnMetricsMiddleware())
    return create_agent(middleware=mw, **kwargs)
