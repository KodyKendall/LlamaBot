"""Per-turn handle for starting work that outlives the turn (background research
and coding tasks).

``delegate_research`` used to make the main turn wait for the sub-agent — 100s
in the report that started this — and any message sent meanwhile cancelled it.
Now the tool hands the research to the thread's RunManager and returns at once.

The request handler installs a launcher with :func:`start_background_launcher`
before ``astream`` (node tasks inherit the context, as in
:mod:`app.lib.turn_notices`). The launcher is
``launcher(*, kind, label, work) -> task_id | None``: ``work`` is
``async () -> str``, and its result is pushed back to the thread as a new turn.
``None`` (no launcher, or the thread is at its cap) means "run it inline" —
scheduled jobs and API callers have no RunManager to own a background task.
"""

from contextvars import ContextVar
from typing import Callable, Optional

_current_launcher: ContextVar[Optional[Callable]] = ContextVar(
    "llamabot_background_launcher", default=None
)


def start_background_launcher(launcher: Optional[Callable]):
    """Install this turn's launcher. Returns the ContextVar token."""
    return _current_launcher.set(launcher)


def get_background_launcher() -> Optional[Callable]:
    return _current_launcher.get()


def suspend_background_launcher():
    """Hide the launcher (for a nested sub-agent, which needs its results inline)."""
    return _current_launcher.set(None)


def restore_background_launcher(token) -> None:
    _current_launcher.reset(token)


# ------------------------------------------------------------ writer gate
#
# A coding task (background delegate_task) edits the project's files while the
# user keeps chatting. Only one writer at a time: the main agent's file-writing
# tools await this gate first, which returns once no coding task is running
# (box-wide — every thread edits the same files). ``gate`` is ``async () -> None``,
# installed per turn next to the launcher; ``run_delegation`` hides it, since a
# sub-agent is the writer and would wait on itself.

_current_writer_gate: ContextVar[Optional[Callable]] = ContextVar(
    "llamabot_writer_gate", default=None
)


def start_writer_gate(gate: Optional[Callable]):
    """Install this turn's writer gate. Returns the ContextVar token."""
    return _current_writer_gate.set(gate)


def current_writer_gate() -> Optional[Callable]:
    return _current_writer_gate.get()


def suspend_writer_gate():
    """Hide the gate (for a nested sub-agent)."""
    return _current_writer_gate.set(None)


def restore_writer_gate(token) -> None:
    _current_writer_gate.reset(token)
