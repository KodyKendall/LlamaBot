"""
Lease Manager for Leonardo instance lifecycle.

Background task that:
- Checks user activity every 5 minutes
- Calls mothership to renew lease if user is active
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.services.mothership_client import MothershipClient

logger = logging.getLogger(__name__)


class LeaseManager:
    """Background task that manages lease renewal based on user activity."""

    CHECK_INTERVAL = 300  # 5 minutes
    ACTIVITY_THRESHOLD = 600  # 10 minutes - user must have activity within this window

    def __init__(self, app: "FastAPI", mothership_client: "MothershipClient"):
        self.app = app
        self.mothership = mothership_client
        self._task = None
        self._running = False

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
                await self._check_and_renew()
            except Exception as e:
                logger.error(f"LeaseManager error: {e}", exc_info=True)

            await asyncio.sleep(self.CHECK_INTERVAL)

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
                self._sync_instance_lock(result)
        else:
            logger.info(
                f"LeaseManager: User inactive ({seconds_since_activity:.0f}s ago), "
                f"not renewing lease"
            )

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
