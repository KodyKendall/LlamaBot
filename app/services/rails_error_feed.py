"""Reads the Rails app's recent-crash feed (``GET /llama_bot/errors``).

Served by ``LlamaBotRails::ErrorsController`` in the llama_bot_rails gem, which
keeps the last 25 exceptions in memory with a monotonic ``seq``. We poll rather
than being pushed to because Rails does not know LlamaBot's URL, and because
polling also catches crashes in background jobs and in requests the user's
browser never made.

Contract, and the reason it is this blunt: this runs once per model call on
every turn. **Every failure returns None** — an expired token, a Rails restart,
a box on a gem that predates the endpoint, a garbled body. None means "no
information", the middleware stays quiet, and the turn proceeds exactly as it
would have. Nothing in here may raise into a turn.

See docs/dev/rails_auto_recovery.md.
"""
import hashlib
import hmac
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.getenv("RAILS_BASE_URL", "http://llamapress:3000")
ENDPOINT_PATH = "/llama_bot/errors"

# Short on purpose. A slow answer is worth less than a fast turn, and the
# request is local to the Docker network.
TIMEOUT_SECONDS = 1.0

# The box-internal credential. Both containers are handed the same
# SECRET_KEY_BASE from the same .env, so each side derives the same value and
# nothing has to be provisioned.
#
# This replaced authenticating as the signed-in Rails user. That version read
# the per-user `api_token` off the WebSocket frame, which only exists while the
# human holds a Devise session in the browser tab — so the feature silently
# no-opped for anyone signed out (2026-08-24: "no Rails api_token on the
# frame"). Whether the box can read its OWN error log must not hinge on a
# browser session.
#
# Plain HMAC-SHA256, matching OpenSSL::HMAC.hexdigest on the Ruby side. Not
# ActiveSupport's MessageVerifier: that is Marshal-based and cannot be
# reproduced here.
FEED_TOKEN_PURPOSE = b"llamabot-error-feed"
FEED_SCHEME = "LlamaBotFeed"


def feed_token() -> Optional[str]:
    """The box-internal feed credential, or None if there is no shared secret."""
    secret = os.getenv("SECRET_KEY_BASE", "").strip()
    if not secret:
        return None
    return hmac.new(secret.encode(), FEED_TOKEN_PURPOSE, hashlib.sha256).hexdigest()


class RailsErrorFeedClient:
    """One-call client for the Rails recent-crash feed."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = TIMEOUT_SECONDS,
        client_factory=None,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token
        self.timeout = timeout
        # Injectable so tests drive an httpx.MockTransport instead of a socket.
        self._client_factory = client_factory or (
            lambda timeout: httpx.AsyncClient(timeout=timeout)
        )

    async def fetch(
        self, *, since: Optional[int], within: Optional[int] = None
    ) -> Optional[Tuple[int, List[Dict[str, Any]]]]:
        """Return ``(cursor, errors)``, or None if we learned nothing.

        ``since`` is the steady state: everything after a cursor we hold. It
        always wins — once armed, re-sending a window would re-deliver crashes
        the turn has already been told about.

        ``within=<seconds>`` (with ``since=None``) is the arming call: what has
        crashed recently. Needed because the common case is an app that is
        ALREADY broken when the user asks for a fix — nothing new is coming, so
        a cursor alone would stay silent while they stare at an error page.

        Neither is a bare cursor probe.
        """
        authorization = self._authorization()
        if authorization is None:
            return None

        if since is not None:
            params = {"since": str(int(since))}
        elif within is not None:
            params = {"within": str(int(within))}
        else:
            params = {}

        try:
            async with self._client_factory(self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}{ENDPOINT_PATH}",
                    params=params,
                    headers={"Authorization": authorization},
                )
                if response.status_code != 200:
                    logger.debug(
                        "rails error feed: HTTP %s (gem too old, or token expired)",
                        response.status_code,
                    )
                    return None
                payload = response.json()
        except Exception as exc:  # noqa: BLE001 - must never break a turn
            logger.debug("rails error feed unavailable: %s", exc)
            return None

        return self._parse(payload)

    def _authorization(self) -> Optional[str]:
        """Box credential first, per-user token as a fallback, else nothing.

        The fallback keeps this working on a box whose Rails container does not
        share SECRET_KEY_BASE (an ejected app), where the signed-in user's token
        is the only credential there is.
        """
        box = feed_token()
        if box:
            return f"{FEED_SCHEME} {box}"
        if self.token:
            return f"LlamaBot {self.token}"
        return None

    @staticmethod
    def _parse(payload: Any) -> Optional[Tuple[int, List[Dict[str, Any]]]]:
        if not isinstance(payload, dict):
            return None

        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return None

        errors = payload.get("errors", [])
        if not isinstance(errors, list):
            return None

        return seq, [entry for entry in errors if isinstance(entry, dict)]
