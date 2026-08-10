"""Routes for connecting a user's ChatGPT (Codex) account to this Leo instance.

Device-code flow: ``/start`` returns a short code and a URL, the user approves on
their own machine, ``/poll`` completes the exchange. See
``app/services/chatgpt_auth.py`` for the protocol and
``docs/dev/chatgpt_oauth_byo_subscription.md`` for the design.

Nothing here ever returns token material — only connection status.
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.db import get_session
from app.dependencies import get_current_user
from app.models import User
from app.services import chatgpt_auth, codex_cli_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chatgpt-auth", tags=["chatgpt-auth"])

# In-flight device-code logins, keyed by an opaque state token. In-process is
# sufficient: a login is completed within 15 minutes by the same container, and a
# restart mid-login just means the user clicks Connect again.
_PENDING: dict = {}
_PENDING_TTL_SECONDS = chatgpt_auth.DEVICE_CODE_TIMEOUT_SECONDS


def _sweep_pending() -> None:
    now = time.time()
    for state in [s for s, v in _PENDING.items() if v["expires_at"] < now]:
        _PENDING.pop(state, None)


class PollRequest(BaseModel):
    state: str


@router.get("/status")
def status(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Whether this user has a working ChatGPT connection. Never returns tokens."""
    return chatgpt_auth.status_for_user(session, user.id)


@router.post("/start")
async def start(user: User = Depends(get_current_user)):
    """Begin a device-code login. Returns the code the user types into OpenAI."""
    _sweep_pending()

    # Preferred path: OpenAI's own CLI. It is the only client that reliably gets
    # past the Cloudflare challenge on auth.openai.com, and it owns refresh too,
    # so a login here keeps working instead of dying at first expiry.
    if codex_cli_auth.cli_available():
        try:
            device = await codex_cli_auth.start_device_login(user.id)
        except codex_cli_auth.CodexCliError as e:
            logger.warning("Codex CLI device login failed: %s", e)
            raise HTTPException(status_code=502, detail=str(e))
        state = chatgpt_auth.new_state_token()
        _PENDING[state] = {
            "user_id": user.id,
            "via": "codex_cli",
            "expires_at": time.time() + _PENDING_TTL_SECONDS,
        }
        return {
            "state": state,
            "user_code": device["user_code"],
            "verification_url": device["verification_url"],
            "interval": device["interval"],
        }

    try:
        device = await chatgpt_auth.request_device_code()
    except chatgpt_auth.AuthHostBlocked as e:
        # Distinct from a generic failure so the UI can switch to the paste flow
        # instead of telling the user to retry something that cannot work.
        logger.warning("ChatGPT sign-in blocked at the network layer: %s", e)
        raise HTTPException(
            status_code=503,
            detail={"reason": "auth_host_blocked", "message": str(e)},
        )
    except chatgpt_auth.DeviceCodeError as e:
        logger.warning("ChatGPT device-code start failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    state = chatgpt_auth.new_state_token()
    _PENDING[state] = {
        "user_id": user.id,
        "device_auth_id": device["device_auth_id"],
        "user_code": device["user_code"],
        "expires_at": time.time() + _PENDING_TTL_SECONDS,
    }
    return {
        "state": state,
        "user_code": device["user_code"],
        "verification_url": device["verification_url"],
        "interval": device["interval"],
    }


@router.post("/poll")
async def poll(
    body: PollRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """One poll of a pending login. ``pending`` until the user approves."""
    _sweep_pending()
    entry = _PENDING.get(body.state)
    if entry is None:
        raise HTTPException(status_code=404, detail="Login expired or not found. Start again.")
    # The state token is the capability, but bind it to its owner anyway so a
    # leaked token can't attach someone else's ChatGPT account to this user.
    if entry["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="This login belongs to another user.")

    if entry.get("via") == "codex_cli":
        try:
            blob = await codex_cli_auth.poll_device_login(user.id)
        except codex_cli_auth.CodexCliError as e:
            _PENDING.pop(body.state, None)
            raise HTTPException(status_code=502, detail=str(e))
        if blob is None:
            return {"status": "pending"}
        try:
            chatgpt_auth.import_credential(session, user.id, blob)
        except chatgpt_auth.DeviceCodeError as e:
            _PENDING.pop(body.state, None)
            raise HTTPException(status_code=502, detail=str(e))
        _PENDING.pop(body.state, None)
        return {"status": "connected", **chatgpt_auth.status_for_user(session, user.id)}

    try:
        result = await chatgpt_auth.poll_device_code(
            entry["device_auth_id"], entry["user_code"]
        )
    except chatgpt_auth.DeviceCodeError as e:
        _PENDING.pop(body.state, None)
        raise HTTPException(status_code=502, detail=str(e))

    if result is None:
        return {"status": "pending"}

    try:
        tokens = await chatgpt_auth.exchange_authorization_code(
            result["authorization_code"], result["code_verifier"]
        )
        chatgpt_auth.save_credential(session, user.id, tokens)
    except chatgpt_auth.DeviceCodeError as e:
        _PENDING.pop(body.state, None)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        _PENDING.pop(body.state, None)
        logger.warning("Could not store ChatGPT credential: %s", e)
        raise HTTPException(status_code=500, detail="Could not store credential.")

    _PENDING.pop(body.state, None)
    return {"status": "connected", **chatgpt_auth.status_for_user(session, user.id)}


class ImportRequest(BaseModel):
    """Either the whole ~/.codex/auth.json, or the token fields from it."""

    auth_json: Optional[dict] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None


@router.post("/import")
def import_credential(
    body: ImportRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Import a credential obtained with OpenAI's Codex CLI on another machine.

    Needed wherever this server's egress IP is refused by ``auth.openai.com``
    (Cloudflare 403) while the inference host stays reachable — see
    ``chatgpt_auth.AuthHostBlocked``.
    """
    blob = body.auth_json or {
        "access_token": body.access_token,
        "refresh_token": body.refresh_token,
        "id_token": body.id_token,
    }
    try:
        return chatgpt_auth.import_credential(session, user.id, blob)
    except chatgpt_auth.DeviceCodeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("ChatGPT credential import failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not store credential.")


@router.delete("/disconnect")
async def disconnect(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Forget this user's ChatGPT tokens."""
    removed = chatgpt_auth.delete_credential(session, user.id)
    # Also drop the CLI's own copy — leaving it behind would silently re-connect
    # the user on the next refresh.
    await codex_cli_auth.cancel_device_login(user.id)
    codex_cli_auth.forget(user.id)
    return {"disconnected": removed}
