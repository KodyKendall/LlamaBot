"""Who may run which tool, and how many, on a ZDR box (LP-33, HECVAT AILM-01, AILM-04).

Both are off on a normal box. Both are checked when a tool runs, from the hook in
:mod:`app.agents.leonardo.safety_hooks`, so a raw-node agent or a client's own agent
cannot skip them. :func:`visible_tools` additionally hides refused tools from the model.

The budget lives in a ContextVar holding a shared object. Sub-agents and background
work run in tasks copied from the turn's context, so they spend the same budget.
"""
import logging
import os
from contextvars import ContextVar
from typing import Optional

from app.agents.leonardo import zdr
from app.lib.request_context import current_principal

logger = logging.getLogger(__name__)

DEFAULT_TOOL_CALL_LIMIT = 40
LIMIT_ENV_VAR = "TOOL_CALL_PER_TURN_LIMIT"

CAP_MESSAGE = (
    "I stopped because this message reached the workspace's safety limit of {limit} "
    "tool calls. Send another message to continue from here."
)
NOT_PERMITTED_MESSAGE = (
    "I stopped because the {tool} tool is not enabled for your role on this workspace. "
    "An administrator can enable write and shell tools for your role."
)
BLOCKED_MESSAGE = (
    "This was not sent to the AI model because it contains sensitive data this "
    "workspace blocks ({rules}). Remove it and try again."
)


class TurnSafetyStop(Exception):
    """Ends a turn on purpose. ``str(e)`` is safe to show the user."""


class _Budget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.exhausted = False


_budget: ContextVar[Optional[_Budget]] = ContextVar("llamabot_tool_budget", default=None)


def tool_call_limit() -> int:
    try:
        limit = int(os.environ.get(LIMIT_ENV_VAR, ""))
    except ValueError:
        return DEFAULT_TOOL_CALL_LIMIT
    return limit if limit > 0 else DEFAULT_TOOL_CALL_LIMIT


def start_turn() -> None:
    """Give this user input a fresh budget. Called where each turn starts."""
    _budget.set(_Budget(tool_call_limit()))


def cap_was_hit() -> bool:
    budget = _budget.get()
    return bool(budget and budget.exhausted)


def cap_message() -> str:
    budget = _budget.get()
    return CAP_MESSAGE.format(limit=budget.limit if budget else tool_call_limit())


def _tool_permitted(name: str) -> bool:
    from app import permissions

    role, is_admin, security_only = current_principal()
    if security_only:
        return name in permissions.READ_ONLY_TOOLS
    return permissions.tool_allowed(name, role, is_admin)


def _limits_apply() -> bool:
    """A security admin is read-only on any box; everyone else only under ZDR."""
    return current_principal()[2] or zdr.enforced()


def _tool_name(tool) -> Optional[str]:
    if isinstance(tool, dict):
        return tool.get("name") or (tool.get("function") or {}).get("name")
    return getattr(tool, "name", None)


def visible_tools(tools: list) -> list:
    """The tools this turn's user may see. Everything, on a normal box."""
    if not _limits_apply():
        return tools
    return [tool for tool in tools if _tool_permitted(str(_tool_name(tool)))]


def check_tool_call(name: str) -> None:
    """Raise :class:`TurnSafetyStop` if this call may not run. Spends one from the budget."""
    if not _limits_apply():
        return
    if not _tool_permitted(name):
        logger.warning("ZDR: refused tool %s for this role", name)
        raise TurnSafetyStop(NOT_PERMITTED_MESSAGE.format(tool=name))
    if not zdr.enforced():
        return
    budget = _budget.get()
    if budget is None:
        return
    if budget.used >= budget.limit:
        budget.exhausted = True
        logger.warning("ZDR: tool call limit of %s reached", budget.limit)
        raise TurnSafetyStop(CAP_MESSAGE.format(limit=budget.limit))
    budget.used += 1
