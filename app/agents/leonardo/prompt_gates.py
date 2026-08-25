"""One source of truth for "is this tool actually available on this box".

A prompt that documents a tool the registry does not have is a bug class, not a
one-off. `browser_inspect` is gated by the ``enable_browser_inspect`` site
setting, which defaults to off — but both Rails prompts described it
unconditionally, so on a default box the agent dutifully called it and got
``Error: browser_inspect is not a valid tool``. It reported that itself, eight
times across seven boxes, and each time fell back to guessing whether the page
it had just written actually rendered.

The fix is to key BOTH the tool list and the prompt off the same predicate.
Wrap the gated part of a prompt in a marker pair::

    <!--IF:browser_inspect-->
    ### Self-Checking Pages with browser_inspect
    ...
    <!--END:browser_inspect-->

and :func:`apply_prompt_gates` removes the whole block when the gate is off.
Blocks for unknown gate names are left alone (a mothership-delivered prompt
override may name a gate this build has never heard of; silently deleting its
content would be worse than leaving a marker visible).

Gate predicates must fail CLOSED — an unreadable site setting means "the tool is
not there", which is exactly what the tool list concludes, so the two can never
disagree.
"""
import logging
import re
from typing import Callable, Dict

logger = logging.getLogger(__name__)

_BLOCK_RE = re.compile(
    r"[ \t]*<!--\s*IF:(?P<name>[A-Za-z0-9_]+)\s*-->.*?<!--\s*END:(?P=name)\s*-->[ \t]*\n?",
    re.DOTALL,
)


def _browser_inspect_gate() -> bool:
    from app.agents.leonardo.rails_agent.tools import browser_inspect_enabled
    return browser_inspect_enabled()


def _live_browser_tools_gate() -> bool:
    from app.agents.leonardo.rails_agent.tools import live_browser_tools_enabled
    return live_browser_tools_enabled()


# name -> the SAME predicate the tool list uses. Imported lazily inside each
# predicate: this module is imported by project_context, which tools.py itself
# reaches through.
GATES: Dict[str, Callable[[], bool]] = {
    "browser_inspect": _browser_inspect_gate,
    "live_browser_tools": _live_browser_tools_gate,
}


def gate_is_open(name: str) -> bool:
    predicate = GATES.get(name)
    if predicate is None:
        return True  # unknown gate: leave the block alone
    try:
        return bool(predicate())
    except Exception:  # noqa: BLE001 - fail closed, exactly like the tool list
        logger.warning("Prompt gate %r could not be read; treating it as off.", name)
        return False


def apply_prompt_gates(prompt: str) -> str:
    """Strip every ``<!--IF:name-->...<!--END:name-->`` block whose gate is off.

    Open gates keep their content and lose only the markers, so an enabled tool
    reads exactly as it did before this existed.
    """
    if not prompt or "<!--IF:" not in prompt.replace(" ", ""):
        return prompt

    open_cache: Dict[str, bool] = {}

    def _replace(match: re.Match) -> str:
        name = match.group("name")
        if name not in open_cache:
            open_cache[name] = gate_is_open(name)
        if not open_cache[name]:
            return ""
        body = match.group(0)
        body = re.sub(r"[ \t]*<!--\s*IF:%s\s*-->[ \t]*\n?" % re.escape(name), "", body)
        body = re.sub(r"[ \t]*<!--\s*END:%s\s*-->[ \t]*\n?" % re.escape(name), "", body)
        return body

    return _BLOCK_RE.sub(_replace, prompt)
