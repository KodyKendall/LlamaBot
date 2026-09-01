"""
Lease Manager for Leonardo instance lifecycle.

Background task that:
- Checks user activity every 5 minutes
- Calls mothership to renew lease if user is active
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.services.mothership_client import MothershipClient

logger = logging.getLogger(__name__)


class LeaseManager:
    """Background task that manages lease renewal based on user activity."""

    CHECK_INTERVAL = 300  # 5 minutes
    ACTIVITY_THRESHOLD = 600  # 10 minutes - user must have activity within this window

    #: The customer's Rails app, by docker service name. Resolves from inside the
    #: llamabot container with no new env var and no new configuration.
    RAILS_HEALTH_URL = "http://llamapress:3000/up"

    #: Deliberately generous. Leo boxes run RAILS_ENV=development, so the first
    #: request after any file save pays the whole class reload — and the agent
    #: saves files all day. A HEALTHY box measured 10.045s on /up while / answered
    #: in 1.37s moments later (crm-4, 2026-08-31). A short timeout here would
    #: report healthy boxes as down several times an hour and make the signal
    #: worthless. Report the real status and duration; let the mothership judge.
    RAILS_PROBE_TIMEOUT = 25.0

    #: How often the box spends a real completion checking its own default model.
    #: Hourly, not every tick: a retirement is permanent, so finding it within the
    #: hour is fast enough, and 148 boxes probing every 5 minutes is pointless
    #: spend. `GET /v1/models` is NOT a substitute — it still listed the retired
    #: model right through the 2026-08-31 outage.
    MODEL_PROBE_INTERVAL = 3600

    def __init__(self, app: "FastAPI", mothership_client: "MothershipClient"):
        self.app = app
        self.mothership = mothership_client
        self._task = None
        self._running = False
        self._last_model_probe = None

    async def start(self):
        """Start the background lease renewal task."""
        if not self.mothership.enabled:
            logger.info("LeaseManager: Mothership not configured, skipping")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"LeaseManager: Started (check every {self.CHECK_INTERVAL}s, "
            f"activity threshold {self.ACTIVITY_THRESHOLD}s)"
        )

    async def stop(self):
        """Stop the background task gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("LeaseManager: Stopped")

    async def _run_loop(self):
        """Main loop: check activity every 5 minutes."""
        # Initial delay to let app fully start
        await asyncio.sleep(10)

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"LeaseManager error: {e}", exc_info=True)

            await asyncio.sleep(self.CHECK_INTERVAL)

    async def _tick(self):
        """One pass of the loop: report Rails health, then renew the lease.

        Health is reported on EVERY tick, deliberately outside _check_and_renew.
        Lease renewal is gated on ACTIVITY_THRESHOLD, so folding the health report
        into it would silence idle boxes — and an idle box that stops answering is
        precisely the blind spot this closes (SI#417).

        Health reporting is telemetry. Lease renewal keeps a customer's box alive.
        The first must never be able to stop the second, so it is fully contained.
        """
        try:
            rails_status, rails_ms = await self._probe_rails()
            health_result = await self.mothership.report_rails_health(
                rails_status=rails_status, rails_ms=rails_ms
            )
            # Policy rides this response as well as the lease one. Lease renewal is
            # gated on ACTIVITY_THRESHOLD, so an idle box would otherwise never receive
            # a model change — and most of the fleet was idle when the default model was
            # retired at 22:46 UTC on 2026-08-31. This call has no such gate.
            if health_result:
                self._sync_model_policy(health_result)
        except Exception as e:
            logger.warning(f"LeaseManager: Rails health report failed: {e}")

        # Rate-limited, and contained for the same reason as the health report:
        # a model probe is diagnostics, lease renewal keeps the box alive.
        try:
            now = time.monotonic()
            if (self._last_model_probe is None
                    or now - self._last_model_probe >= self.MODEL_PROBE_INTERVAL):
                self._last_model_probe = now
                await self._probe_default_model()
        except Exception as e:
            logger.warning(f"LeaseManager: model probe failed: {e}")

        await self._check_and_renew()

    async def _probe_rails(self):
        """Ask the customer's Rails app whether it is answering.

        Returns (status_code, milliseconds). A wedged Rails answers nothing at all,
        including its own health endpoint, so "no answer" is reported as status 0
        rather than swallowed — that is the case worth alerting on.
        """
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.RAILS_PROBE_TIMEOUT) as client:
                response = await client.get(self.RAILS_HEALTH_URL)
                status = response.status_code
        except Exception:
            status = 0

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return status, elapsed_ms

    async def _check_and_renew(self):
        """If user was active in last 10 minutes, renew lease."""
        last_activity = getattr(self.app.state, 'timestamp', None)
        if last_activity is None:
            logger.warning("LeaseManager: No timestamp in app.state")
            return

        now = datetime.now(timezone.utc)
        seconds_since_activity = (now - last_activity).total_seconds()

        if seconds_since_activity <= self.ACTIVITY_THRESHOLD:
            logger.info(
                f"LeaseManager: User active ({seconds_since_activity:.0f}s ago), "
                f"renewing lease..."
            )
            result = await self.mothership.renew_lease()
            if result:
                logger.info(
                    f"LeaseManager: Lease renewed until {result.get('lease_expires_at')}"
                )
                self._sync_model_policy(result)
                self._sync_instance_lock(result)
        else:
            logger.info(
                f"LeaseManager: User inactive ({seconds_since_activity:.0f}s ago), "
                f"not renewing lease"
            )

    async def _probe_default_model(self):
        """Spend one real completion checking the model this box would actually run.

        A completion, not ``GET /v1/models``: when Meta retired
        ``muse-spark-1.2-contributor`` on 2026-08-31 the model list STILL LISTED it,
        so every list-reading health check reported green through the whole outage.
        Only a real call tells the truth.

        A 404 feeds the same dead-model memory the live ladder uses, so the box
        reroutes itself before a customer types anything. Any OTHER failure is
        ignored on purpose — a timeout or a 500 is the provider having a bad
        minute, and condemning a model on that would let one blip move the fleet.
        """
        from app.agents.leonardo import model_health
        from app.agents.leonardo.llm_factory import get_llm
        from app.agents.leonardo.model_policy import enabled_default_model
        from app.agents.leonardo.resilience import is_model_gone

        model = enabled_default_model()
        try:
            llm = get_llm(model)
            await llm.ainvoke([{"role": "user", "content": "ping"}])
            logger.debug(f"LeaseManager: default model {model} answered the probe")
        except Exception as e:
            if is_model_gone(e):
                logger.error(
                    f"LeaseManager: default model {model} reports itself RETIRED "
                    f"({e!r}). Routing around it; set DEFAULT_LLM_MODEL to choose "
                    f"the replacement."
                )
                model_health.mark_model_gone(model)
            else:
                logger.info(
                    f"LeaseManager: model probe for {model} failed without saying "
                    f"the model is gone ({e!r}); not condemning it"
                )

    def _sync_model_policy(self, result: dict) -> None:
        """Apply a model policy the mothership sent down with the lease renewal.

        Meta retired the compiled default model on 2026-08-31 while it carried 89%
        of fleet turns, and the only way to move a box off it was to SSH in and
        edit .env. This is the channel that makes it one fleet-wide action.

        Deliberately a PULL riding the lease response rather than a new inbound
        endpoint: no new listening surface, no second credential, and it reuses
        the authenticated call the box already makes. Same shape as the instance
        lock below, for the same reason.

        No-op unless the mothership actually sends the key, so an older mothership
        never clears a policy — and absent is not the same as empty. Fully
        contained: telemetry must never be able to break lease renewal.
        """
        if "model_policy" not in result:
            return
        try:
            from app.services import model_policy_store

            policy = result["model_policy"]
            if policy:
                model_policy_store.save(policy)
            else:
                # An explicit empty policy IS a clear — the mothership handing the
                # box back to its own configuration.
                model_policy_store.clear()
        except Exception as e:
            logger.warning(f"LeaseManager: could not apply remote model policy: {e}")

    def _sync_instance_lock(self, result: dict) -> None:
        """Backstop for the sleep lock when the mothership can't reach us inbound.

        ``POST /api/instance-lock`` is the fast path (instant). This picks the
        same state off the lease-renew response, so a lock still lands within a
        check interval if that POST failed. No-op unless the mothership actually
        sends the key — an older mothership just never triggers it.
        """
        if "instance_lock" not in result:
            return
        try:
            from sqlmodel import Session

            from app.db import engine
            from app.services.instance_lock import set_lock_state

            payload = result["instance_lock"] or {}
            with Session(engine) as session:
                state = set_lock_state(session, payload)
            self.app.state.instance_lock = state
            logger.info(f"LeaseManager: instance lock synced from mothership (locked={state['locked']})")
        except Exception as e:
            logger.warning(f"LeaseManager: could not sync instance lock: {e}")
