"""Hooks on the LangChain base classes for the ZDR safety limits (LP-33).

Agents here are built several ways: create_agent with shared middleware, raw graph
nodes, sub-agents, and clients' own agents from the overlay. A check in any one of
those misses the others, and "misses" here means customer data reaches a vendor. Every
chat model and every tool goes through these two base classes, so that is where the
checks sit. They do nothing unless ZDR is on — except the export guard (LP-45), which
is always on: it refuses shell commands that take the whole app or database off the box.

``_convert_input`` and ``run``/``arun`` are LangChain internals. If an upgrade moves
them, ``tests/test_ai_safety_limits.py`` fails rather than the filter going quiet.
"""
import functools

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_core.prompt_values import ChatPromptValue
from langchain_core.tools import BaseTool

from app.agents.leonardo import export_rules, sensitive_data, tool_limits, zdr

_installed = False


def _filter_prompt(prompt):
    if not zdr.enforced():
        return prompt
    try:
        return ChatPromptValue(messages=sensitive_data.sanitize_messages(prompt.to_messages()))
    except sensitive_data.SensitiveDataBlocked as blocked:
        raise tool_limits.TurnSafetyStop(tool_limits.BLOCKED_MESSAGE.format(rules=blocked)) from None


def _export_refusal(tool, args, kwargs):
    """The refusal to return instead of running ``tool``, or None to run it (LP-45).

    Returned as the tool's result, not raised: the turn goes on and the model passes
    the plain message on to the user.
    """
    tool_input = args[0] if args else kwargs.get("tool_input")
    pattern = export_rules.refused_tool_call(tool.name, tool_input)
    if pattern is None:
        return None
    export_rules.log_refusal(tool.name, pattern)
    tool_call_id = kwargs.get("tool_call_id")
    if tool_call_id:
        return ToolMessage(export_rules.REFUSAL_MESSAGE, tool_call_id=tool_call_id,
                           name=tool.name, status="error")
    return export_rules.REFUSAL_MESSAGE


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    convert_input = BaseChatModel._convert_input

    @functools.wraps(convert_input)
    def _convert_input(self, model_input):
        return _filter_prompt(convert_input(self, model_input))

    BaseChatModel._convert_input = _convert_input

    run = BaseTool.run
    arun = BaseTool.arun

    @functools.wraps(run)
    def _run(self, *args, **kwargs):
        refused = _export_refusal(self, args, kwargs)
        if refused is not None:
            return refused
        tool_limits.check_tool_call(self.name)
        return run(self, *args, **kwargs)

    @functools.wraps(arun)
    async def _arun(self, *args, **kwargs):
        refused = _export_refusal(self, args, kwargs)
        if refused is not None:
            return refused
        tool_limits.check_tool_call(self.name)
        return await arun(self, *args, **kwargs)

    BaseTool.run = _run
    BaseTool.arun = _arun
