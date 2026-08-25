"""Puts a live Rails crash in front of the model, mid-turn.

The model-call boundary is the injection point for one reason: it is the only
place LangGraph lets you add a message to a run that is already in flight
without cancelling and restarting it, and it is the point the model actually
reads. Leo finishes the tool it is running, and before its next thought it sees
that the app just crashed — functionally the same as the user pasting the error
in, except the user never had to see the broken page.

Design constraints, each pinned by a test in
``app/tests/test_rails_error_watch_middleware.py``:

1. **Inert without a watch.** Headless runs and tests never installed one, so
   the request is passed through untouched and nothing is polled.
2. **Never fatal.** A crash-recovery feature that can crash a turn is worse than
   no feature. Every failure path returns the original request.
3. **Transparent.** The handler's result is returned unmodified.
4. **Protocol-safe.** Never appends after an unanswered ``tool_calls`` message.

All the "should we say something" logic lives in ``app/lib/rails_error_watch``
so it can be tested without LangGraph. See docs/dev/rails_auto_recovery.md.
"""
import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from app.lib.rails_error_watch import ARMING_WINDOW_SECONDS, current_error_watch

logger = logging.getLogger(__name__)


def _default_feed_factory(watch):
    from app.services.rails_error_feed import RailsErrorFeedClient

    return RailsErrorFeedClient(token=watch.api_token)


class RailsErrorWatchMiddleware(AgentMiddleware):
    """Polls the Rails error feed once per model call and injects what is new."""

    def __init__(self, *, feed_factory=None):
        super().__init__()
        # Injectable so tests drive a fake feed instead of HTTP.
        self._feed_factory = feed_factory or _default_feed_factory
        self._explained = None

    @staticmethod
    def _has_unanswered_tool_calls(messages) -> bool:
        if not messages:
            return False
        last = messages[-1]
        return isinstance(last, AIMessage) and bool(getattr(last, "tool_calls", None))

    async def _maybe_inject(self, request):
        watch = current_error_watch()
        if watch is None:
            self._explain_once(None, "no watch installed for this turn")
            return request
        if not watch.armed:
            self._explain_once(watch, f"agent {watch.agent_name!r} is read-only")
            return request

        arming = watch.cursor is None

        feed = self._feed_factory(watch)
        result = await feed.fetch(
            since=watch.cursor,
            within=ARMING_WINDOW_SECONDS if arming else None,
        )
        if result is None:
            self._explain_once(watch, "error feed unreachable (old gem, 403, or Rails down)")
            # Rails is restarting, the gem predates the endpoint, or the token
            # expired. Stay unarmed and try again on the next model call.
            return request

        cursor, errors = result
        watch.prime(cursor) if arming else watch.advance(cursor)
        self._explain_once(
            watch, f"armed, cursor={watch.cursor}, {len(errors)} recent error(s)"
        )

        # On the arming call these crashes predate the turn — the user asked for
        # help with an app that was already down. Report them, but do not tell
        # the agent it caused something it could not have.
        report = watch.plan_injection(errors, pre_existing=arming)
        if not report:
            return request

        messages = list(request.messages)
        if self._has_unanswered_tool_calls(messages):
            # The tool results are still coming. Injecting here would break the
            # tool-call protocol; the next model call picks this up instead —
            # the fingerprint is unspent because plan_injection already ran, so
            # re-report it explicitly rather than dropping it on the floor.
            watch.seen_fingerprints.difference_update(
                {e.get("fingerprint") for e in errors if e.get("fingerprint")}
            )
            watch.injections = max(0, watch.injections - 1)
            return request

        messages.append(HumanMessage(content=report))
        logger.info(
            "rails auto-recovery: injected crash report on thread %s (attempt %s)",
            watch.thread_id,
            watch.injections,
        )
        return request.override(messages=messages)

    def _explain_once(self, watch, reason):
        """Say once per turn why auto-recovery is or is not going to speak up.

        Without this the feature is undiagnosable from the outside: a quiet turn
        looks identical whether the watch was disarmed, the feed was
        unreachable, or there was simply nothing wrong.
        """
        key = id(watch) if watch is not None else "none"
        if self._explained == key:
            return
        self._explained = key
        logger.info("rails auto-recovery: %s", reason)

    async def _safe_inject(self, request):
        try:
            return await self._maybe_inject(request)
        except Exception as exc:  # noqa: BLE001 - must never break a turn
            logger.debug("rails auto-recovery skipped: %s", exc)
            return request

    async def awrap_model_call(self, request, handler):
        return await handler(await self._safe_inject(request))

    def wrap_model_call(self, request, handler):
        # The sync path exists only because AgentMiddleware declares it. Every
        # Leonardo agent runs through astream, so polling here would mean
        # blocking HTTP on the model-call path for no reachable benefit.
        return handler(request)
