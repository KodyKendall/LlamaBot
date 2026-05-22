"""GitHub Device Flow OAuth API routes."""

import logging
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.dependencies import engineer_or_admin_required
from app.models import User

logger = logging.getLogger(__name__)
router = APIRouter()

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")


def _get_client_id():
    """Get GitHub Client ID, checking env at call time (not import time)."""
    return os.getenv("GITHUB_CLIENT_ID", "")


@router.post("/api/github/device-code", response_class=JSONResponse)
async def start_device_flow(
    current_user: User = Depends(engineer_or_admin_required),
):
    """Start GitHub device flow - returns user_code and verification_uri."""
    client_id = _get_client_id()
    if not client_id:
        raise HTTPException(status_code=500, detail="GITHUB_CLIENT_ID not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/device/code",
            data={"client_id": client_id, "scope": "repo"},
            headers={"Accept": "application/json"},
        )

    if resp.status_code != 200:
        logger.error(f"GitHub device code request failed: {resp.text}")
        raise HTTPException(status_code=502, detail="Failed to start GitHub device flow")

    data = resp.json()
    return {
        "user_code": data["user_code"],
        "verification_uri": data["verification_uri"],
        "device_code": data["device_code"],
        "expires_in": data["expires_in"],
        "interval": data.get("interval", 5),
    }


@router.post("/api/github/poll-auth", response_class=JSONResponse)
async def poll_device_auth(
    request: dict,
    current_user: User = Depends(engineer_or_admin_required),
):
    """Poll GitHub for device flow completion. Returns token on success."""
    device_code = request.get("device_code")
    if not device_code:
        raise HTTPException(status_code=400, detail="device_code required")

    client_id = _get_client_id()
    if not client_id:
        raise HTTPException(status_code=500, detail="GITHUB_CLIENT_ID not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="GitHub token request failed")

    data = resp.json()

    # GitHub returns error while waiting
    if "error" in data:
        error = data["error"]
        if error == "authorization_pending":
            return {"status": "pending"}
        elif error == "slow_down":
            return {"status": "slow_down", "interval": data.get("interval", 10)}
        elif error == "expired_token":
            return {"status": "expired"}
        elif error == "access_denied":
            return {"status": "denied"}
        else:
            return {"status": "error", "message": data.get("error_description", error)}

    # Success - we got a token
    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="No access_token in GitHub response")

    # Install the token on the host via gh auth login --with-token
    install_result = _install_gh_token(access_token)

    return {
        "status": "success",
        "message": install_result.get("message", "GitHub connected!"),
        "install_success": install_result.get("success", False),
    }


def _install_gh_token(token: str) -> dict:
    """Install GitHub token on host using gh auth login --with-token via nsenter."""
    import subprocess

    # Reuse the execute_command pattern from slash_commands
    from app.routers.slash_commands import execute_command, is_native_linux_host

    try:
        # Step 1: Login with the token on the host
        login_cmd = f"echo '{token}' | gh auth login --with-token"
        result = execute_command(login_cmd, timeout=30)

        if result.returncode != 0:
            logger.error(f"gh auth login failed: {result.stderr}")
            return {"success": False, "message": f"gh auth login failed: {result.stderr}"}

        # Step 2: Setup git credential helper
        setup_cmd = "gh auth setup-git"
        execute_command(setup_cmd, timeout=15)

        # Step 3: Copy creds to containers (if on Linux host)
        if is_native_linux_host():
            copy_cmd = (
                "docker compose exec -T llamabot mkdir -p /root/.config/gh && "
                "docker compose exec -T code mkdir -p /config/.config/gh && "
                "docker compose cp /home/ubuntu/.config/gh/. llamabot:/root/.config/gh/ && "
                "docker compose cp /home/ubuntu/.config/gh/. code:/config/.config/gh/ && "
                "docker compose exec -T llamabot gh auth setup-git && "
                "docker compose exec -T code gh auth setup-git"
            )
            copy_result = execute_command(copy_cmd, timeout=60)
            if copy_result.returncode != 0:
                logger.warning(f"gh credential copy had issues: {copy_result.stderr}")
                return {
                    "success": True,
                    "message": "GitHub authenticated on host! Container credential copy had warnings."
                }

        return {"success": True, "message": "GitHub authenticated and credentials synced!"}

    except Exception as e:
        logger.error(f"Error installing gh token: {e}")
        return {"success": False, "message": f"Token install error: {str(e)}"}


@router.get("/api/github/status", response_class=JSONResponse)
async def github_auth_status(
    current_user: User = Depends(engineer_or_admin_required),
):
    """Check if GitHub is already authenticated on the host."""
    import subprocess
    from app.routers.slash_commands import execute_command

    try:
        result = execute_command("gh auth status 2>&1", timeout=10)
        authenticated = result.returncode == 0
        return {
            "authenticated": authenticated,
            "output": result.stdout.strip() if result.stdout else result.stderr.strip(),
        }
    except Exception as e:
        return {"authenticated": False, "output": str(e)}
