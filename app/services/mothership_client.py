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
