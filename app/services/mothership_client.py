"""
Mothership API client for Leonardo instance management.

Handles communication with the LlamaPressLeo mothership for:
- Lease renewal (keep instance alive when user is active)
- Graceful teardown notification (on SIGTERM)
"""

import httpx
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class MothershipClient:
    """HTTP client for calling mothership endpoints."""

    CONFIG_PATH = ".leonardo/instance.json"
    TIMEOUT = 30.0

    def __init__(self):
        self.config = self._load_config()

    def _load_config(self) -> Optional[dict]:
        """Load config from instance.json, return None if not found."""
        try:
            with open(self.CONFIG_PATH) as f:
                config = json.load(f)
                logger.info(f"Loaded mothership config for instance: {config.get('instance_name')}")
                return config
        except FileNotFoundError:
            logger.info("No instance.json found - mothership integration disabled")
            return None
        except IsADirectoryError:
            # Docker bind-mounts a nonexistent source path as a directory, so in
            # some compose environments .leonardo/instance.json is an empty dir.
            # Treat that the same as "not configured" instead of crashing at boot.
            logger.info("instance.json is a directory (no config mounted) - mothership integration disabled")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in instance.json: {e}")
            return None

    @property
    def enabled(self) -> bool:
        """Check if mothership integration is enabled."""
        return (
            self.config is not None
            and self.config.get("mothership_api_token")
            and self.config.get("mothership_url")
            and self.config.get("instance_name")
        )

    @property
    def instance_name(self) -> Optional[str]:
        """Get instance name from config."""
        return self.config.get("instance_name") if self.config else None

    @property
    def mothership_url(self) -> Optional[str]:
        """Get the mothership base URL from config (e.g. https://llamapress.ai)."""
        return self.config.get("mothership_url") if self.config else None

    @property
    def lease_duration_seconds(self) -> Optional[int]:
        """Get lease duration from config."""
        return self.config.get("lease_duration_seconds") if self.config else None

    async def renew_lease(self) -> Optional[dict]:
        """
        POST /api/leonardo/lease_renew

        Called when user is active to extend the instance lease.
        Returns the new lease expiration time.
        """
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/lease_renew",
                    json={"instance_name": self.config["instance_name"]},
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"}
                )
                response.raise_for_status()
                result = response.json()
                logger.info(f"Lease renewed: expires at {result.get('lease_expires_at')}")
                return result
        except httpx.HTTPStatusError as e:
            logger.error(f"Lease renewal failed (HTTP {e.response.status_code}): {e.response.text}")
            return None
        except httpx.RequestError as e:
            logger.error(f"Lease renewal request failed: {e}")
            return None

    async def report_message(
        self,
        *,
        thread_id: str,
        role: str,
        content: str,
        sent_at: str,
        model: Optional[str] = None,
        token_usage: Optional[dict] = None,
        tool_calls: Optional[list] = None,
        tool_call_id: Optional[str] = None,
        timings: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        POST /api/leonardo/report_message

        Reports each user message and each finalized top-level assistant reply
        to the mothership for usage analytics. Never raises — failures return None.

        For role="user", the response includes paywall fields
        ({allowed_next, messages_remaining}) which callers use to populate
        the local paywall cache. For role="assistant", the response contains
        no paywall fields.

        ``timings`` (assistant messages only) carries the duration / TTFT /
        tokens-per-second of the model call that produced this reply, so the
        mothership has a per-message performance series alongside token usage.
        See docs/dev/performance_telemetry.md.
        """
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                payload = {
                    "instance_name": self.config["instance_name"],
                    "thread_id": thread_id,
                    "role": role,
                    "content": content,
                    "sent_at": sent_at,
                }
                if model:
                    payload["model"] = model
                if token_usage:
                    payload["token_usage"] = token_usage
                if tool_calls:
                    payload["tool_calls"] = tool_calls
                if tool_call_id:
                    payload["tool_call_id"] = tool_call_id
                if timings:
                    payload["timings"] = timings
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/report_message",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
                )
                response.raise_for_status()
                body = response.json()
                logger.info(f"report_message response (role={role}): {body}")
                return body
        except httpx.HTTPStatusError as e:
            logger.warning(f"Message report failed (HTTP {e.response.status_code}): {e.response.text}")
            return None
        except httpx.RequestError as e:
            logger.warning(f"Message report request failed: {e}")
            return None
        except Exception as e:
            logger.warning(f"Message report unexpected error: {e}")
            return None

    async def submit_feedback(
        self,
        *,
        thread_id: str,
        rating: str,
        scope: str = "message",
        note: Optional[str] = None,
        content: Optional[str] = None,
        sent_at: Optional[str] = None,
        debug_context: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        POST /api/leonardo/submit_feedback

        End-user 👍/👎 on a single AI message (scope="message") or the whole
        session (scope="session"). Lands on the mothership as an
        InstanceMessageAnnotation tagged source="end_user".

        Best-effort, exactly like report_message: never raises, returns None on
        any failure so a reporting hiccup never blocks the chat.
        """
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                payload = {
                    "instance_name": self.config["instance_name"],
                    "thread_id": thread_id,
                    "rating": rating,
                    "scope": scope,
                }
                if note:
                    payload["note"] = note
                if content:
                    payload["content"] = content
                if sent_at:
                    payload["sent_at"] = sent_at
                if debug_context:
                    payload["debug_context"] = debug_context
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/submit_feedback",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
                )
                response.raise_for_status()
                body = response.json()
                logger.info(f"submit_feedback response (scope={scope}, rating={rating}): {body}")
                return body
        except httpx.HTTPStatusError as e:
            logger.warning(f"submit_feedback failed (HTTP {e.response.status_code}): {e.response.text}")
            return None
        except httpx.RequestError as e:
            logger.warning(f"submit_feedback request failed: {e}")
            return None
        except Exception as e:
            logger.warning(f"submit_feedback unexpected error: {e}")
            return None

    async def check_paywall(self) -> Optional[dict]:
        """
        POST /api/leonardo/check_paywall

        Recheck-on-blocked path: called only when the cached paywall state
        says blocked, to detect "user just paid / daily reset" transitions.
        Returns {"allowed": bool, "messages_remaining": int|None} or None on
        any error. Fail-open contract — callers treat None as "allow".
        """
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/check_paywall",
                    json={"instance_name": self.config["instance_name"]},
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
                )
                response.raise_for_status()
                body = response.json()
                logger.info(f"check_paywall response: {body}")
                return body
        except Exception as e:
            logger.warning(f"Paywall recheck failed, failing open: {e}")
            return None

    async def check_updates(self, current_llamabot: str, current_llamapress: str) -> Optional[dict]:
        """
        POST /api/leonardo/check_updates

        Check mothership for newer stable versions of llamabot and llamapress.
        Returns {"updates_available": bool, "latest_versions": {...}} or None on error.

        Also carries the versioned-system-prompt round-trip: we send the versions
        we have cached (keyed by agent_mode == langgraph graph key) and persist any
        prompt bodies the mothership returns under `system_prompts`. Fully fail-open
        and backwards-compatible — an old mothership simply omits `system_prompts`.
        """
        if not self.enabled:
            return None

        try:
            from app.services import system_prompt_cache

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/check_updates",
                    json={
                        "instance_name": self.config["instance_name"],
                        "current_versions": {
                            "llamabot": current_llamabot,
                            "llamapress": current_llamapress,
                        },
                        "prompt_versions": system_prompt_cache.cached_versions(),
                    },
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
                )
                response.raise_for_status()
                body = response.json()
                logger.info(f"check_updates response: {body}")

                # Persist any delivered system prompts (only changed/new modes are sent).
                for mode, payload in (body.get("system_prompts") or {}).items():
                    version = (payload or {}).get("version")
                    text = (payload or {}).get("body")
                    if version and text:
                        system_prompt_cache.upsert(mode, version, text)

                return body
        except Exception as e:
            logger.warning(f"Update check failed: {e}")
            return None

    async def report_error(
        self,
        *,
        thread_id: Optional[str],
        error_class: str,
        error_message: str,
        traceback_str: str,
        agent_mode: Optional[str] = None,
        model: Optional[str] = None,
        llamabot_version: Optional[str] = None,
        occurred_at: Optional[str] = None,
        fingerprint: Optional[str] = None,
        recovered: Optional[bool] = None,
        source: str = "llamabot",
    ) -> None:
        """
        POST /api/leonardo/report_error

        Fire-and-forget telemetry for errors that reached an end user (the outer
        catch in websocket/request_handler.py, and any rung of the resilience
        ladder). This closes the visibility gap: an instance can finally tell the
        mothership that a user hit an error, with the model / agent_mode /
        version needed to triage it (see docs/dev/error_telemetry.md).

        Best-effort, exactly like report_disconnect: never raises, returns None
        on any failure so a reporting hiccup never worsens the error the user
        already saw. ``recovered`` distinguishes "handled by the graceful floor"
        from "hard failure the user is stuck on".
        """
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                payload = {
                    "instance_name": self.config["instance_name"],
                    "error_class": error_class,
                    "error_message": (error_message or "")[:2000],
                    "traceback": (traceback_str or "")[:5000],
                    # Receiver allowlists %w[llamabot rails_app frontend]; anything
                    # else still defaults to "llamabot" on the mothership side.
                    "source": source or "llamabot",
                }
                if thread_id:
                    payload["thread_id"] = thread_id
                if agent_mode:
                    payload["agent_mode"] = agent_mode
                if model:
                    payload["model"] = model
                if llamabot_version:
                    payload["llamabot_version"] = llamabot_version
                if occurred_at:
                    payload["occurred_at"] = occurred_at
                if fingerprint:
                    payload["fingerprint"] = fingerprint
                if recovered is not None:
                    payload["recovered"] = recovered
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/report_error",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
                )
                response.raise_for_status()
                return None
        except httpx.HTTPStatusError as e:
            logger.warning(f"Error report failed (HTTP {e.response.status_code}): {e.response.text}")
            return None
        except httpx.RequestError as e:
            logger.warning(f"Error report request failed: {e}")
            return None
        except Exception as e:
            logger.warning(f"Error report unexpected error: {e}")
            return None

    async def report_turn_metrics(
        self,
        *,
        thread_id: Optional[str],
        metrics: dict,
        agent_mode: Optional[str] = None,
        model: Optional[str] = None,
        llamabot_version: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        """
        POST /api/leonardo/report_turn_metrics

        End-of-turn performance rollup: where the wall clock actually went
        (model wait vs. tool execution vs. graph/checkpointer overhead), plus
        the turn's TTFT and decode rate. This is what makes "Leo is slow"
        diagnosable instead of anecdotal — see docs/dev/performance_telemetry.md
        for the payload contract and docs/handoff_mothership_performance.md for
        the receiving end.

        Deliberately ONE request per turn, not per event: a tool-heavy turn
        already fires dozens of report_message calls, and fleet-wide ingest cost
        is the constraint that decides whether this can stay switched on.

        Fire-and-forget like the rest of the telemetry surface — never raises,
        always returns None. The turn is already over and the user has their
        answer; a metrics failure must be invisible to them.
        """
        if not self.enabled:
            return None

        # A turn that recorded nothing (e.g. interrupted before the first model
        # call) has nothing to report, and an empty row would drag fleet
        # averages toward zero.
        if not metrics:
            return None

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                payload = {
                    "instance_name": self.config["instance_name"],
                    "metrics": metrics,
                }
                if thread_id:
                    payload["thread_id"] = thread_id
                if agent_mode:
                    payload["agent_mode"] = agent_mode
                if model:
                    payload["model"] = model
                if llamabot_version:
                    payload["llamabot_version"] = llamabot_version
                if occurred_at:
                    payload["occurred_at"] = occurred_at
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/report_turn_metrics",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
                )
                response.raise_for_status()
                return None
        except httpx.HTTPStatusError as e:
            # A 404 here just means the mothership hasn't shipped the endpoint
            # yet; debug-level so an un-upgraded mothership can't spam logs.
            logger.debug(f"Turn metrics report failed (HTTP {e.response.status_code}): {e.response.text}")
            return None
        except httpx.RequestError as e:
            logger.debug(f"Turn metrics report request failed: {e}")
            return None
        except Exception as e:
            logger.debug(f"Turn metrics report unexpected error: {e}")
            return None

    async def verify_login_grant(
        self, token: str, audience: str = "llamabot"
    ) -> "tuple[Optional[dict], Optional[str]]":
        """
        POST /api/leonardo/verify_login_grant

        Unified Login Phase 2: redeem a short-lived opaque grant the mothership
        minted, server-to-server (no shared secret). Unlike the fire-and-forget
        telemetry methods, the CALLER must distinguish outcomes to drive the
        consume flow, so this returns a ``(payload, error_code)`` tuple rather
        than logging-and-None:

          * success        -> ``(body, None)`` where body carries user/role/
            permissions/link_username (see app/routers/unified_login.py).
          * server refusal -> ``(None, error_code)`` with the mothership's code
            (``grant_not_found`` / ``grant_expired`` / ``grant_used`` /
            ``bad_audience``). ``grant_expired`` / ``grant_used`` are EXPECTED on
            refresh/bookmark and the caller degrades gracefully.
          * transport fail -> ``(None, "mothership_unreachable")`` — timeouts,
            connection refused, a drifted 401, or a disabled client. A retry
            bounce can't help these, so the caller goes straight to the login
            page.

        Never raises.
        """
        if not self.enabled:
            return None, "mothership_unreachable"

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/verify_login_grant",
                    json={
                        "instance_name": self.config["instance_name"],
                        "token": token,
                        "audience": audience,
                    },
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"},
                )

            if response.status_code == 200:
                body = response.json()
                if body.get("success"):
                    return body, None
                # 200 with success:false — honor the server's code if present.
                return None, body.get("error_code") or "verify_failed"

            # Non-200: prefer the server's structured error_code (404/409/410/422
            # all carry one). A 401 means the box's mothership creds drifted — no
            # error_code, and a bounce can't fix it, so treat it as unreachable.
            if response.status_code == 401:
                return None, "mothership_unreachable"
            try:
                code = (response.json() or {}).get("error_code")
            except Exception:
                code = None
            return None, code or f"http_{response.status_code}"
        except httpx.RequestError as e:
            logger.warning(f"verify_login_grant request failed: {e}")
            return None, "mothership_unreachable"
        except Exception as e:
            logger.warning(f"verify_login_grant unexpected error: {e}")
            return None, "mothership_unreachable"

    async def notify_teardown(self, reason: str = "sigterm") -> Optional[dict]:
        """
        POST /api/leonardo/teardown

        Called on SIGTERM to notify mothership to initiate backup and termination.
        """
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                response = await client.post(
                    f"{self.config['mothership_url']}/api/leonardo/teardown",
                    json={
                        "instance_name": self.config["instance_name"],
                        "reason": reason
                    },
                    headers={"Authorization": f"Bearer {self.config['mothership_api_token']}"}
                )
                response.raise_for_status()
                result = response.json()
                logger.info(f"Teardown notification sent: {result.get('message', 'success')}")
                return result
        except httpx.HTTPStatusError as e:
            logger.error(f"Teardown notification failed (HTTP {e.response.status_code}): {e.response.text}")
            return None
        except httpx.RequestError as e:
            logger.error(f"Teardown notification request failed: {e}")
            return None
