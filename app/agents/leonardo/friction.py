"""Agent-reported friction ("papercuts") → the mothership Error Queue.

Error telemetry has always been about things that *crashed*: a Python exception
in the request handler, a browser-side socket drop. But most of what slows a Leo
down never raises anything — a tool errors in a way its description never warned
about, a file is root-owned so every edit silently fails, output contradicts the
docs, a capability just isn't there and has to be routed around. That all died in
the transcript, and the only way we ever learned about it was a customer
complaining downstream.

`report_friction` is the channel for it. The agent files a short, structured
complaint about its OWN tooling; we ship it down the SAME pipe as real errors
(`MothershipClient.report_error`, see `docs/dev/error_telemetry.md`) so it lands
in the existing Error Queue with no new plumbing — tagged `source="agent_friction"`
so self-reported papercuts can be filtered apart from genuine exceptions.

Three properties this module protects, in priority order:

1. **It can never hurt the turn.** Bad enum values are coerced, not rejected; a
   broken runtime is swallowed; the network POST happens on a detached daemon
   thread so the agent never waits on it. The tool always returns a string.
2. **It can't flood the queue.** A retry loop would otherwise file the same
   papercut twenty times. Reports are deduped by fingerprint and capped per
   thread (:data:`MAX_REPORTS_PER_THREAD`).
3. **It doesn't derail the agent.** Every return string — including the dropped
   ones — reads as "noted, keep going", never as an error the model should retry.

NOTE for whoever picks up the mothership side: `/api/leonardo/report_error`
allowlists `source` to %w[llamabot rails_app frontend] and silently defaults
anything else to "llamabot". Until `agent_friction` is added there, these reports
land in the queue but wear the wrong source label. `error_class` is
`AgentFriction.<category>`, so they're still greppable in the meantime.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

from langchain.tools import tool, ToolRuntime

logger = logging.getLogger(__name__)

# Tag on the mothership row. Distinct from "llamabot" on purpose: mixing
# self-reported papercuts into the exception stream would wreck triage.
FRICTION_SOURCE = "agent_friction"

FRICTION_CATEGORIES = (
    "tool_error",          # a tool failed in a way its description didn't warn about
    "permissions",         # ownership / read-only / EACCES
    "confusing_output",    # misleading, truncated, or contradictory output
    "missing_capability",  # needed something that doesn't exist
    "environment",         # container, network, service, dependency
    "docs_mismatch",       # the description and the behaviour disagree
    "slow",                # unreasonably slow to the point of changing the approach
    "other",
)
FRICTION_SEVERITIES = (
    "blocked",     # could not complete the task
    "workaround",  # got there, but routed around it
    "annoyance",   # cost time or clarity
)
DEFAULT_CATEGORY = "other"
DEFAULT_SEVERITY = "annoyance"

# Budget per thread. Deliberately small: three good papercuts per conversation is
# signal, thirty is a log file nobody reads.
MAX_REPORTS_PER_THREAD = 3
# Bound on the dedupe table so a long-lived process can't grow it forever.
MAX_TRACKED_THREADS = 200

MAX_WHAT_HAPPENED = 2000
MAX_EVIDENCE = 4000
MAX_SUGGESTED_FIX = 500
MAX_DETAILS = 5000  # matches report_error's own traceback cap


REPORT_FRICTION_DESCRIPTION = """Report a papercut: something in YOUR OWN tools or environment that got in your way.

This is telemetry for the LlamaPress team — the engineers who build and fix your
tools. The user never sees it. It does NOT fix your problem and it does NOT change
what you should do next. File it and keep working.

Call this when:
- A tool returns an error you did not expect, or fails in a way its description
  never warned you about.
- You have to retry the same call more than twice to get it to work.
- You hit a permission, ownership, or read-only failure.
- A tool's output is confusing, misleading, truncated, or contradicts its own
  description.
- You need a capability that does not exist and have to work around its absence.
- Something in the environment is broken, missing, or slow enough to change your
  approach.

Do NOT call this for:
- Bugs in the USER's application code. That is the work, not friction.
- Your own mistakes (a wrong path, bad syntax) that you fixed on the next try.
- Anything you want the user to know. Tell them directly instead.

Parameters:
- what_happened: 1-3 plain sentences — what you tried, what happened, and how it
  got in your way. Write it for an engineer who cannot see this conversation.
- category: one of tool_error, permissions, confusing_output, missing_capability,
  environment, docs_mismatch, slow, other
- severity: one of
    blocked     - you could not complete the task
    workaround  - you got there, but had to route around it
    annoyance   - it cost you time or clarity
- tool_name: (optional) the tool involved, e.g. "edit_file".
- evidence: (optional) the literal error text, command, or output. Paste it
  verbatim — this is the single most useful field in the report.
- suggested_fix: (optional) one sentence on what would have made this work.

Report each distinct problem once. Repeats within a conversation are dropped, and
there is a small cap per conversation — spend it on the things that actually cost
you something."""


FRICTION_PROMPT_SECTION = """

---

## Reporting Friction (report_friction)

You have a `report_friction` tool. Use it when YOUR OWN tools or environment get in
your way: a tool errors in a way its description never warned about, a permission or
ownership failure, output that is confusing or contradicts its docs, a capability you
needed that does not exist, or something broken or slow in the environment. If you
find yourself retrying the same call over and over, or inventing a workaround for
something that should have just worked, that is exactly what this is for.

Three rules:

1. This goes to the LlamaPress team, who fix your tools. It is invisible to the user.
   Never mention it to them, never apologise for it, and never make them wait on it.
2. It does not fix anything for you right now. Report it in one call and immediately
   continue with what you were doing.
3. Report friction in YOUR tooling only. A bug in the user's own application is the
   work itself — fix that, don't report it.

Be concrete and paste the literal error into `evidence`. One report per distinct
problem."""


def with_friction_section(prompt: str) -> str:
    """Append the friction instructions to a built system prompt (idempotent).

    Applied AFTER the project-context overlay rather than baked into the mode's
    prompt constant, so a mothership-delivered prompt override (which replaces the
    base prompt wholesale — see ``project_context.resolve_base_prompt``) can never
    silently drop the instructions for a tool the agent still has.
    """
    if not prompt:
        return FRICTION_PROMPT_SECTION
    if FRICTION_PROMPT_SECTION in prompt:
        return prompt
    return prompt + FRICTION_PROMPT_SECTION


def friction_fingerprint(category: str, tool_name: Optional[str], what_happened: str) -> str:
    """Stable id for one distinct papercut.

    Same md5 shape the backend and frontend already use, so a future mothership
    change could dedupe friction rows the same way it dedupes error rows.
    """
    first_line = ((what_happened or "").splitlines() or [""])[0]
    return hashlib.md5(
        f"{category}|{tool_name or ''}|{first_line[:160]}".encode("utf-8", "replace")
    ).hexdigest()


def _coerce(value: Optional[str], allowed: tuple[str, ...], default: str) -> str:
    """Snap a model-supplied enum onto the allowed set. Never raises.

    A wrong enum is not worth failing a tool call over — the model would just
    retry, burning a turn on telemetry.
    """
    candidate = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return candidate if candidate in allowed else default


def _truncate(text: Optional[str], limit: int) -> str:
    """Clamp to ``limit`` INCLUDING the marker, so callers can trust the cap.

    report_error truncates again on its own limits; producing something longer
    than the documented cap here would just get silently cut mid-marker.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    marker = f"\n... [truncated, {len(text)} chars total]"
    return text[: max(0, limit - len(marker))] + marker


def _llamabot_version() -> Optional[str]:
    try:
        from app.routers.api import get_container_version
        return get_container_version()
    except Exception:
        return None


def build_friction_report(
    *,
    what_happened: str,
    category: str,
    severity: str,
    thread_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    evidence: Optional[str] = None,
    suggested_fix: Optional[str] = None,
    agent_mode: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Normalize a raw tool call into the payload the mothership receives."""
    category = _coerce(category, FRICTION_CATEGORIES, DEFAULT_CATEGORY)
    severity = _coerce(severity, FRICTION_SEVERITIES, DEFAULT_SEVERITY)
    what_happened = _truncate(what_happened, MAX_WHAT_HAPPENED)
    tool_name = (tool_name or "").strip() or None

    lines = [
        f"severity: {severity}",
        f"category: {category}",
        f"tool: {tool_name or '(not specified)'}",
        f"agent_mode: {agent_mode or '(unknown)'}",
        f"model: {model or '(unknown)'}",
    ]
    if evidence:
        lines += ["", "--- evidence ---", _truncate(evidence, MAX_EVIDENCE)]
    if suggested_fix:
        lines += ["", "--- suggested fix ---", _truncate(suggested_fix, MAX_SUGGESTED_FIX)]

    return {
        "what_happened": what_happened,
        "category": category,
        "severity": severity,
        "tool_name": tool_name,
        "thread_id": thread_id,
        "agent_mode": agent_mode,
        "model": model,
        # Named so a queue full of real crashes stays readable at a glance, and so
        # `AgentFriction.` is one grep away even before the mothership learns the
        # new source value.
        "error_class": f"AgentFriction.{category}",
        "details": _truncate("\n".join(lines), MAX_DETAILS),
        "fingerprint": friction_fingerprint(category, tool_name, what_happened),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "llamabot_version": _llamabot_version(),
    }


# ---------------------------------------------------------------------------
# Per-thread budget (dedupe + cap)
# ---------------------------------------------------------------------------

_tracking_lock = threading.Lock()
_thread_fingerprints: "OrderedDict[str, set]" = OrderedDict()


def reset_friction_tracking() -> None:
    """Forget every thread's budget. Test seam."""
    with _tracking_lock:
        _thread_fingerprints.clear()


def claim_report_slot(thread_key: str, fingerprint: str) -> tuple[bool, str]:
    """Decide whether this report gets sent. Returns (accepted, reason)."""
    with _tracking_lock:
        seen = _thread_fingerprints.get(thread_key)
        if seen is None:
            seen = set()
            _thread_fingerprints[thread_key] = seen
            while len(_thread_fingerprints) > MAX_TRACKED_THREADS:
                _thread_fingerprints.popitem(last=False)
        else:
            _thread_fingerprints.move_to_end(thread_key)

        if fingerprint in seen:
            return False, "duplicate"
        if len(seen) >= MAX_REPORTS_PER_THREAD:
            return False, "capped"
        seen.add(fingerprint)
        return True, "accepted"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

async def send_friction_report(report: dict, mothership: Any = None) -> bool:
    """POST one friction report through the existing error pipeline.

    Best-effort, exactly like ``report_error`` itself: never raises, returns False
    on any failure. A telemetry hiccup must never worsen the turn the agent is
    already in.
    """
    try:
        if mothership is None:
            from app.services.mothership_client import MothershipClient
            mothership = MothershipClient()

        if not mothership.enabled:
            logger.info("Friction report dropped: mothership integration disabled")
            return False

        await mothership.report_error(
            thread_id=report.get("thread_id"),
            error_class=report["error_class"],
            error_message=report["what_happened"],
            traceback_str=report["details"],
            agent_mode=report.get("agent_mode"),
            model=report.get("model"),
            llamabot_version=report.get("llamabot_version"),
            occurred_at=report.get("occurred_at"),
            fingerprint=report.get("fingerprint"),
            # "blocked" is the only severity where the agent did NOT get past it.
            recovered=report.get("severity") != "blocked",
            source=FRICTION_SOURCE,
        )
        logger.info(
            f"Reported agent friction: {report['error_class']} "
            f"({report.get('severity')}, tool={report.get('tool_name')})"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to report agent friction (non-fatal): {e}")
        return False


def dispatch_friction_report(report: dict) -> None:
    """Fire-and-forget the POST on a detached daemon thread.

    Tools run on a worker thread with no running event loop, so the coroutine
    can't just be scheduled — and awaiting it inline would make the agent sit on
    a network round trip to file a complaint. A daemon thread with its own loop
    is both simpler and impossible to block on.
    """
    def _run() -> None:
        try:
            asyncio.run(send_friction_report(report))
        except Exception as e:  # pragma: no cover - defence in depth
            logger.warning(f"Friction report thread failed (non-fatal): {e}")

    threading.Thread(target=_run, name="friction-report", daemon=True).start()


def _state_get(state: Any, key: str) -> Optional[str]:
    """Read a field from graph state, which may be a dict or an object."""
    if state is None:
        return None
    if isinstance(state, dict):
        return state.get(key)
    return getattr(state, key, None)


@tool(description=REPORT_FRICTION_DESCRIPTION)
def report_friction(
    what_happened: str,
    # `str`, not `Literal[...]`, on purpose. A Literal would put a real enum in the
    # schema the model sees — better first-try accuracy — but langchain would then
    # REJECT an off-list value before this function ever runs, turning a typo into a
    # tool error the model has to retry. Burning a turn on telemetry is worse than
    # a mislabelled report, so we take any string and snap it in `_coerce`. The
    # allowed values are enumerated in the description.
    category: str,
    severity: str,
    runtime: ToolRuntime,
    tool_name: Optional[str] = None,
    evidence: Optional[str] = None,
    suggested_fix: Optional[str] = None,
) -> str:
    """File a papercut against our own tooling. Never fails the turn."""
    try:
        state = getattr(runtime, "state", None)
        config = getattr(runtime, "config", None) or {}
        thread_id = (config.get("configurable") or {}).get("thread_id")

        report = build_friction_report(
            what_happened=what_happened,
            category=category,
            severity=severity,
            thread_id=str(thread_id) if thread_id is not None else None,
            tool_name=tool_name,
            evidence=evidence,
            suggested_fix=suggested_fix,
            agent_mode=_state_get(state, "agent_mode"),
            model=_state_get(state, "llm_model"),
        )

        accepted, reason = claim_report_slot(
            str(thread_id or "unknown"), report["fingerprint"]
        )
        if not accepted:
            # Phrased as "noted", never as an error — an error string invites the
            # model to retry, which is the exact loop the cap exists to stop.
            if reason == "duplicate":
                return (
                    "Already reported this one in this conversation, so it was not sent "
                    "again. Nothing for you to do — continue with your task."
                )
            return (
                f"Friction budget for this conversation is used up "
                f"({MAX_REPORTS_PER_THREAD} reports). Not sent. Nothing for you to do — "
                "continue with your task."
            )

        dispatch_friction_report(report)
        return (
            f"Friction recorded ({report['category']} / {report['severity']}) and sent to "
            "the LlamaPress team. This does not fix the problem and the user cannot see "
            "it — continue with your task."
        )
    except Exception as e:
        logger.warning(f"report_friction failed (non-fatal): {e}")
        return (
            "Friction report could not be filed. This is not your problem to solve — "
            "continue with your task."
        )
