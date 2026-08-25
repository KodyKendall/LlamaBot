"""Per-turn state for mid-turn Rails auto-recovery.

When agent-written code crashes the Rails app, the user should never be the one
who discovers it. ``RailsErrorWatchMiddleware`` polls the Rails error feed once
per model call and hands whatever is new to :meth:`RailsErrorWatch.plan_injection`,
which decides — with no HTTP and no LangGraph in the way — whether to put a
message in front of the model.

Everything that bounds the fix/break/fix loop lives in this module, because that
loop is the failure mode the feature invites:

* the cursor is primed on the FIRST model call, so only crashes caused *during
  this turn* can inject;
* a fingerprint is reported at most once per turn, so a render loop firing the
  same ``NoMethodError`` 200 times costs one message;
* after :data:`MAX_INJECTIONS_PER_TURN` attempts the next error produces a
  "stop and explain to the user" note instead, and then the watch goes quiet;
* the message is capped at :data:`MAX_REPORT_CHARS` so a 400-frame backtrace
  cannot eat the context window.

Installed per turn in a ``ContextVar`` exactly like ``app/lib/turn_metrics.py``:
the request handler calls :func:`start_error_watch`, and the middleware inside
LangGraph's node tasks resolves to the same object (tasks inherit a copy of the
context).

See docs/dev/rails_auto_recovery.md.
"""
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

# Three swings at it. Past that, a fourth failure is far more likely to be the
# agent thrashing than one more typo, and the user is better served by being
# told what is broken than by watching another attempt.
MAX_INJECTIONS_PER_TURN = 3

# Hard ceiling on one injected message.
MAX_REPORT_CHARS = 4000

# Per error. The first few frames name the file to open; the rest is framework.
MAX_BACKTRACE_LINES = 12

# How far back the arming call looks. Long enough to catch "I broke it, then
# typed a message", short enough that a crash from earlier in the session does
# not get dredged into an unrelated turn.
ARMING_WINDOW_SECONDS = 120

_HEADER = (
    "[automated] The Rails app crashed while you were working. The user has not "
    "seen this yet, and it happened AFTER this turn started — so it is very "
    "likely caused by a change you just made.\n\n"
)

_HEADER_PRE_EXISTING = (
    "[automated] The Rails app is throwing an error RIGHT NOW — the user's "
    "preview is showing an error page instead of their app. This started "
    "before your turn, so it is not necessarily something you did; do not "
    "revert work to chase it. Diagnose it from the trace below.\n\n"
)

_GIVE_UP = (
    "[automated] The Rails app is still crashing after "
    f"{MAX_INJECTIONS_PER_TURN} repair attempts this turn. Stop trying to fix "
    "it. Tell the user in plain language what is broken and what you already "
    "tried, and let them decide what to do next."
)

_TRUNCATED = "\n… (report truncated)"

_current_watch: ContextVar[Optional["RailsErrorWatch"]] = ContextVar(
    "rails_error_watch", default=None
)

# Watches also live in a small LRU keyed by thread, because a ContextVar cannot
# span two WebSocket frames and an interrupt/resume cycle is exactly that. The
# cap is the whole cleanup story: a watch is replaced when its thread's next
# user message arrives, and otherwise falls off the end. Each holds a handful of
# ints and strings.
MAX_TRACKED_THREADS = 50

_WATCHES: "OrderedDict[str, RailsErrorWatch]" = OrderedDict()


def agent_can_auto_recover(agent_name: Optional[str]) -> bool:
    """True for agent modes that are allowed to act on a crash report.

    Plan-mode agents are read-only by design: handing one a crash it is not
    permitted to fix produces an apology, not a repair.
    """
    if not agent_name:
        return False
    return "plan_mode" not in str(agent_name)


def _footer(attempt: int) -> str:
    return (
        "\n\nFix this now, before you finish this turn: diagnose it, edit the "
        "file, then reload the page to confirm it renders. This is attempt "
        f"{attempt} of {MAX_INJECTIONS_PER_TURN} — if you cannot fix it, stop "
        "and tell the user plainly what is broken."
    )


def _format_entry(index: int, entry: Dict[str, Any]) -> Optional[str]:
    """One error as a block, or None if the entry carries nothing usable."""
    error_class = str(entry.get("error_class") or "").strip()
    message = str(entry.get("message") or "").strip()
    if not error_class and not message:
        return None

    lines = [f"{index}) {error_class}: {message}".rstrip(": ")]

    method = str(entry.get("method") or "").strip()
    path = str(entry.get("path") or "").strip()
    if path:
        where = f"   at {method} {path}".rstrip()
        try:
            count = int(entry.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        if count > 1:
            where += f"   (raised {count}x)"
        lines.append(where)

    backtrace = entry.get("backtrace")
    if isinstance(backtrace, list):
        for frame in backtrace[:MAX_BACKTRACE_LINES]:
            lines.append(f"   {frame}")

    return "\n".join(lines)


def _build_report(
    entries: List[Dict[str, Any]], attempt: int, pre_existing: bool = False
) -> Optional[str]:
    header = _HEADER_PRE_EXISTING if pre_existing else _HEADER
    footer = _footer(attempt)
    budget = MAX_REPORT_CHARS - len(header) - len(footer)

    blocks: List[str] = []
    used = 0
    for index, entry in enumerate(entries, start=1):
        block = _format_entry(index, entry)
        if block is None:
            continue

        remaining = budget - used - (1 if blocks else 0)
        if remaining <= len(_TRUNCATED):
            break
        if len(block) > remaining:
            block = block[: remaining - len(_TRUNCATED)] + _TRUNCATED
            blocks.append(block)
            break

        blocks.append(block)
        used += len(block) + (1 if len(blocks) > 1 else 0)

    if not blocks:
        return None
    return header + "\n".join(blocks) + footer


class RailsErrorWatch:
    """Bookkeeping for one turn's worth of Rails crash recovery."""

    def __init__(
        self,
        *,
        thread_id: str,
        agent_name: Optional[str],
        api_token: Optional[str] = None,
    ):
        self.thread_id = thread_id
        self.agent_name = agent_name
        self.api_token = api_token

        # None until the first model call reads where the error log stands.
        self.cursor: Optional[int] = None
        self.injections = 0
        self.gave_up = False
        self.seen_fingerprints: set = set()

    @property
    def armed(self) -> bool:
        """Whether this turn should poll the Rails error feed at all.

        Purely the mode gate. It deliberately does NOT require ``api_token``:
        that per-user credential only exists while the human holds a Devise
        session in the browser, and gating on it meant a signed-out user got no
        crash recovery at all (2026-08-24). The feed client authenticates as the
        box and falls back to ``api_token`` only where there is no box secret.
        """
        return agent_can_auto_recover(self.agent_name)

    def prime(self, seq: int) -> None:
        """Record where the error log stood when this turn began.

        Never moves backwards: a Rails process that restarts mid-turn resets its
        in-memory log to seq 0, and rewinding to that would replay the whole
        ring into the turn.
        """
        if self.cursor is None or seq > self.cursor:
            self.cursor = int(seq)

    def advance(self, seq: int) -> None:
        self.prime(seq)

    def plan_injection(
        self, errors: List[Dict[str, Any]], *, pre_existing: bool = False
    ) -> Optional[str]:
        """The message to put in front of the model, or None to stay quiet.

        ``pre_existing`` means these crashes predate the turn — the user asked
        for help with an app that was already down. Same budget, different
        framing: the agent must not be told it probably caused something it
        could not have.
        """
        if self.gave_up or not errors:
            return None

        fresh = []
        for entry in errors:
            fingerprint = entry.get("fingerprint") or (
                f"{entry.get('error_class')}|{entry.get('message')}|{entry.get('path')}"
            )
            if fingerprint in self.seen_fingerprints:
                continue
            fresh.append((fingerprint, entry))

        if not fresh:
            return None

        if self.injections >= MAX_INJECTIONS_PER_TURN:
            self.gave_up = True
            return _GIVE_UP

        report = _build_report(
            [entry for _, entry in fresh], self.injections + 1, pre_existing=pre_existing
        )
        if report is None:
            return None

        self.injections += 1
        for fingerprint, _ in fresh:
            self.seen_fingerprints.add(fingerprint)
        return report


def start_error_watch(
    *, thread_id: str, agent_name: Optional[str], api_token: Optional[str] = None
) -> RailsErrorWatch:
    """Begin a fresh turn: new cursor, new budget. Called on a user message."""
    watch = RailsErrorWatch(
        thread_id=thread_id, agent_name=agent_name, api_token=api_token
    )
    _remember(thread_id, watch)
    _current_watch.set(watch)
    return watch


def resume_error_watch(*, thread_id: str) -> Optional[RailsErrorWatch]:
    """Re-install the watch for a turn that is continuing after an interrupt.

    A turn is not one WebSocket frame. Leo's own verification step is a
    ``browser_command`` interrupt — the graph pauses, the frontend navigates the
    preview, and the answer arrives as a separate frame that resumes the run.
    That navigation is the single most likely moment for a 500 to surface, so
    the same watch (cursor, budget, fingerprints) has to come back with it.

    Returns None for a thread we never armed; the caller carries on regardless.
    """
    watch = _WATCHES.get(thread_id)
    if watch is None:
        return None
    _WATCHES.move_to_end(thread_id)
    _current_watch.set(watch)
    return watch


def current_error_watch() -> Optional[RailsErrorWatch]:
    return _current_watch.get()


def clear_error_watch() -> None:
    _current_watch.set(None)


def _remember(thread_id: str, watch: RailsErrorWatch) -> None:
    _WATCHES[thread_id] = watch
    _WATCHES.move_to_end(thread_id)
    while len(_WATCHES) > MAX_TRACKED_THREADS:
        _WATCHES.popitem(last=False)


def _reset_registry() -> None:
    """Test hook."""
    _WATCHES.clear()
    _current_watch.set(None)


def _registry_size() -> int:
    """Test hook."""
    return len(_WATCHES)
