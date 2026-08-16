"""Middleware that bounds the size of every tool result.

This is the ``create_agent`` half of the single choke point described in
``app/agents/utils/tool_output_limits.py``; the raw ``StateGraph`` agents get the
same cap from ``CappedToolNode`` at their ``ToolNode`` call sites.

Wired into ``build_leonardo_agent`` rather than into individual agents, so a new
mode — or a new tool inside an existing mode — cannot forget it. That is the
whole point: three incidents in a row came from capping the specific input we
happened to be thinking about, and the next unbounded string finding another
door. A tool result larger than the summarization keep-tail can never be
compacted away, so this is the layer that has to be exhaustive.

Design constraints, same as ``TurnMetricsMiddleware``:

1. **Transparent when nothing is oversized** — the handler's result is returned
   untouched, by identity.
2. **Never fatal.** A cap that raises would turn a slow turn into a dead one, so
   any unexpected result shape passes straight through.
"""
import logging

from langchain.agents.middleware import AgentMiddleware

from app.agents.utils.tool_output_limits import cap_tool_node_output

logger = logging.getLogger(__name__)


class ToolResultSizeLimitMiddleware(AgentMiddleware):
    """Cap any single tool result to ``TOOL_RESULT_MAX_BYTES``."""

    def _cap(self, result):
        try:
            return cap_tool_node_output(result)
        except Exception:  # pragma: no cover - defensive; a cap must never kill a turn
            logger.exception("Tool result size cap failed (non-fatal)")
            return result

    def wrap_tool_call(self, request, handler):
        return self._cap(handler(request))

    async def awrap_tool_call(self, request, handler):
        return self._cap(await handler(request))
