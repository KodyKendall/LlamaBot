"""Reads the Rails app's recurring jobs (``GET /llama_bot/recurring_tasks``).

LP-14. Non-AI scheduled jobs live in the Rails app: Leo writes the job in
``app/jobs/`` and schedules it in ``db/recurring.yml``, and Solid Queue runs it
inside Puma. LlamaBot only shows them, read-only, on the Scheduled Jobs page.

Served by ``LlamaBotRails::RecurringTasksController`` in the llama_bot_rails gem,
behind the same box-internal credential as the crash feed (see
``rails_error_feed.feed_token``).

Every failure — no shared secret, Rails down, a gem that predates the endpoint,
a garbled body — returns ``available: False`` with a short reason, so the page
renders "nothing to show" instead of an error.
"""
import logging
from typing import Any, Dict, Optional

import httpx

from app.services.rails_error_feed import DEFAULT_BASE_URL, FEED_SCHEME, feed_token

logger = logging.getLogger(__name__)

ENDPOINT_PATH = "/llama_bot/recurring_tasks"

# Page load, not a turn — a little more patience than the crash feed.
TIMEOUT_SECONDS = 3.0


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason, "tasks": [], "errors": [], "restart_needed": False}


class RailsRecurringTasksClient:
    """One-call client for the Rails recurring-tasks feed."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout: float = TIMEOUT_SECONDS,
        client_factory=None,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        # Injectable so tests drive an httpx.MockTransport instead of a socket.
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )

    async def fetch(self) -> Dict[str, Any]:
        token = feed_token()
        if not token:
            return _unavailable("LlamaBot and Rails do not share SECRET_KEY_BASE.")

        try:
            async with self._client_factory(self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}{ENDPOINT_PATH}",
                    headers={"Authorization": f"{FEED_SCHEME} {token}"},
                )
        except Exception as exc:  # noqa: BLE001 - the page must still render
            logger.debug("rails recurring tasks unavailable: %s", exc)
            return _unavailable("The Rails app is not responding.")

        if response.status_code == 404:
            return _unavailable("This app's Rails image predates scheduled Rails jobs.")
        if response.status_code != 200:
            return _unavailable(f"The Rails app answered HTTP {response.status_code}.")

        try:
            payload = response.json()
        except ValueError:
            return _unavailable("The Rails app sent an unreadable answer.")

        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            return _unavailable("The Rails app sent an unreadable answer.")

        payload.setdefault("errors", [])
        payload.setdefault("restart_needed", False)
        if not payload.get("available"):
            payload.setdefault("reason", "Solid Queue is not set up in this app.")
        return payload
