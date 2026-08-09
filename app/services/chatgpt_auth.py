"""ChatGPT (Codex) OAuth device-code sign-in, so a user can run Leo on their own plan.

Protocol transcribed from OpenAI's Apache-licensed Codex client
(``openai/codex``, ``codex-rs/login/src/device_code_auth.rs`` and ``server.rs``):

  1. ``POST {issuer}/deviceauth/usercode``  {client_id}
       -> {device_auth_id, user_code, verification_url, interval}
  2. poll ``POST {issuer}/deviceauth/token``  {device_auth_id, user_code}
       403/404 == "not approved yet, keep waiting"; success returns an
       *authorization code* plus its PKCE verifier, not a token
  3. ``POST {issuer}/oauth/token``  grant_type=authorization_code + code_verifier
       -> {access_token, refresh_token, id_token, expires_in}

Device code (rather than the localhost-redirect flow the desktop CLI uses) is the
right fit here: the container has no browser, and the user approves on their own
machine — the token is only ever minted for them, on their instance.

We identify as ``llamabot``, not as the Codex CLI. Codex exposes an originator
override (``CODEX_INTERNAL_ORIGINATOR_OVERRIDE``) and a client-id override
(``CODEX_APP_SERVER_LOGIN_CLIENT_ID``), so honest identification is a supported
path rather than something we have to spoof around. See
``docs/dev/chatgpt_oauth_byo_subscription.md`` §Phase 0.
"""

import base64
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session, select

logger = logging.getLogger(__name__)

# --- Endpoints / identity ----------------------------------------------------

ISSUER = os.getenv("CHATGPT_AUTH_ISSUER", "https://auth.openai.com").rstrip("/")

# OpenAI's public Codex OAuth client (codex-rs/login/src/auth/manager.rs:1618).
# Overridable so a future OpenAI-issued LlamaBot client id is a config change.
CLIENT_ID = os.getenv("CHATGPT_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann")

# We call ourselves what we are. If the backend ever refuses an unknown
# originator that is a product decision (see the plan doc) — we do NOT silently
# fall back to impersonating "codex_cli_rs".
ORIGINATOR = os.getenv("CHATGPT_ORIGINATOR", "llamabot")

# Where inference goes once authenticated (the ChatGPT-plan Codex backend, not
# api.openai.com).
CODEX_BASE_URL = os.getenv(
    "CHATGPT_CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex"
)

DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
_HTTP_TIMEOUT = 30.0

# auth.openai.com sits behind Cloudflare and blocks some datacenter egress IPs
# outright — a bare GET returns a 403 challenge page regardless of client, TLS
# stack or User-Agent. (Verified 2026-08-08 from llamapress-dev, 167.233.45.145:
# auth.openai.com 403, while chatgpt.com/backend-api/codex answered 401 and
# api.openai.com answered 401 — i.e. inference is reachable and only sign-in is
# blocked.)
#
# Two supported ways through, both honest — we do not try to defeat the
# challenge:
#   * CHATGPT_AUTH_PROXY: send only the auth/refresh calls via a proxy whose IP
#     OpenAI accepts. Inference still goes direct.
#   * import_credential(): the user signs in with OpenAI's own Codex CLI on their
#     own machine and hands us the resulting token. Nothing here ever touches
#     auth.openai.com in that mode until a refresh is due.
_AUTH_PROXY = os.getenv("CHATGPT_AUTH_PROXY") or None


def _auth_client(**kwargs) -> httpx.AsyncClient:
    """HTTP client for auth-server calls, honoring CHATGPT_AUTH_PROXY."""
    if _AUTH_PROXY:
        kwargs["proxy"] = _AUTH_PROXY
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT, **kwargs)


class AuthHostBlocked(Exception):
    """auth.openai.com refused this egress IP (Cloudflare 403 challenge page)."""


def _raise_if_blocked(resp) -> None:
    """Turn Cloudflare's HTML challenge into a diagnosable error.

    Without this the caller sees a generic 403 and starts debugging client_id or
    scopes, when the real problem is that the request never reached OpenAI.
    """
    if resp.status_code == 403 and "text/html" in resp.headers.get("content-type", ""):
        raise AuthHostBlocked(
            "auth.openai.com refused THIS HTTP CLIENT with a Cloudflare "
            "challenge (not the IP — OpenAI's own Codex CLI reaches the same "
            "host from the same address). Sign in via the Codex CLI path "
            "instead; see app/services/codex_cli_auth.py."
        )

# Refresh this far ahead of expiry so a long turn can't die mid-stream.
_REFRESH_SKEW = timedelta(minutes=5)

# Key under which the credential-encryption key is stored in the auth DB. Kept
# out of VALID_SITE_SETTINGS so it is never readable or settable through the
# public /api/site-settings surface — exactly like ``session_secret``.
_FERNET_DB_KEY = "chatgpt_credential_key"


def default_headers() -> dict:
    """Headers every Codex-backend call carries."""
    return {
        "originator": ORIGINATOR,
        "User-Agent": f"{ORIGINATOR}/1.0",
    }


# --- Encryption at rest ------------------------------------------------------


def _fernet() -> Optional[Fernet]:
    """Fernet built from a key that survives a container recreate.

    Resolution mirrors ``token_service._ensure_session_secret``:
      1. ``CHATGPT_CREDENTIAL_KEY`` env override.
      2. A row in the auth DB — durable because the postgres volume outlives
         ``bin/update``. Generated once, reused forever after.

    Returns None when no auth DB is reachable and no override is set. Callers
    must treat that as "cannot store credentials", never as "store in clear".
    """
    override = os.getenv("CHATGPT_CREDENTIAL_KEY")
    if override:
        return Fernet(override.encode() if isinstance(override, str) else override)

    try:
        from app.db import engine
        if engine is None:
            return None
        from sqlmodel import SQLModel
        from app.models import SiteSetting
        try:
            SQLModel.metadata.create_all(engine, tables=[SiteSetting.__table__])
        except Exception:
            pass
        with Session(engine) as session:
            row = session.get(SiteSetting, _FERNET_DB_KEY)
            if row and row.value:
                return Fernet(row.value.encode())
            new_key = Fernet.generate_key().decode()
            session.add(SiteSetting(key=_FERNET_DB_KEY, value=new_key))
            try:
                session.commit()
                return Fernet(new_key.encode())
            except Exception:
                # Lost the insert race with another worker — re-read the winner.
                session.rollback()
                row = session.get(SiteSetting, _FERNET_DB_KEY)
                return Fernet(row.value.encode()) if row and row.value else None
    except Exception as e:
        logger.warning("Could not load ChatGPT credential key: %s", e)
        return None


def _encrypt(value: str) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "No encryption key available for ChatGPT credentials "
            "(no auth DB and no CHATGPT_CREDENTIAL_KEY)."
        )
    return f.encrypt(value.encode()).decode()


def _decrypt(value: str) -> Optional[str]:
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        # Key rotated or row written under a different key — treat as disconnected
        # rather than crashing the turn.
        logger.warning("ChatGPT credential could not be decrypted; treating as disconnected.")
        return None


# --- Device-code flow --------------------------------------------------------


class DeviceCodeError(Exception):
    """Raised when OpenAI rejects a device-code step."""


async def request_device_code() -> dict:
    """Step 1: ask OpenAI for a user code the human types into their browser."""
    url = f"{ISSUER}/deviceauth/usercode"
    async with _auth_client() as client:
        resp = await client.post(
            url,
            json={"client_id": CLIENT_ID},
            headers={"Content-Type": "application/json", **default_headers()},
        )
    _raise_if_blocked(resp)
    if resp.status_code == 404:
        raise DeviceCodeError(
            "Device-code login is not enabled for this OpenAI endpoint."
        )
    if resp.status_code >= 400:
        raise DeviceCodeError(
            f"Device code request failed ({resp.status_code}): {resp.text[:200]}"
        )
    data = resp.json()
    return {
        "device_auth_id": data["device_auth_id"],
        "user_code": data["user_code"],
        "verification_url": data.get("verification_url"),
        "interval": int(data.get("interval") or 5),
    }


async def poll_device_code(device_auth_id: str, user_code: str) -> Optional[dict]:
    """Step 2: one poll. Returns the auth code payload, or None if not yet approved.

    Mirrors the Codex client: 403/404 means "still waiting", anything else 4xx/5xx
    is a real failure.
    """
    url = f"{ISSUER}/deviceauth/token"
    async with _auth_client() as client:
        resp = await client.post(
            url,
            json={"device_auth_id": device_auth_id, "user_code": user_code},
            headers={"Content-Type": "application/json", **default_headers()},
        )
    _raise_if_blocked(resp)
    if resp.status_code in (403, 404):
        return None
    if resp.status_code >= 400:
        raise DeviceCodeError(
            f"Device auth failed ({resp.status_code}): {resp.text[:200]}"
        )
    return resp.json()


async def exchange_authorization_code(authorization_code: str, code_verifier: str) -> dict:
    """Step 3: trade the authorization code for access + refresh tokens (PKCE)."""
    url = f"{ISSUER}/oauth/token"
    async with _auth_client() as client:
        resp = await client.post(
            url,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "client_id": CLIENT_ID,
                "code_verifier": code_verifier,
                "redirect_uri": f"{ISSUER}/deviceauth/callback",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                **default_headers(),
            },
        )
    _raise_if_blocked(resp)
    if resp.status_code >= 400:
        raise DeviceCodeError(
            f"Token exchange failed ({resp.status_code}): {resp.text[:200]}"
        )
    return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """Trade a refresh token for a fresh access token."""
    url = os.getenv("CHATGPT_REFRESH_URL") or f"{ISSUER}/oauth/token"
    async with _auth_client() as client:
        resp = await client.post(
            url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                **default_headers(),
            },
        )
    _raise_if_blocked(resp)
    if resp.status_code >= 400:
        raise DeviceCodeError(
            f"Token refresh failed ({resp.status_code}): {resp.text[:200]}"
        )
    return resp.json()


# --- id_token claims ---------------------------------------------------------


def claims_from_id_token(id_token: str) -> dict:
    """Decode the id_token payload WITHOUT verifying (we just received it over TLS
    from the issuer we called). Used only to display the account and pick the
    account-id header — never for an authorization decision.
    """
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def account_info(id_token: Optional[str]) -> dict:
    """Pull account id / plan / email out of an id_token, best-effort."""
    if not id_token:
        return {}
    claims = claims_from_id_token(id_token)
    auth_claim = claims.get("https://api.openai.com/auth") or {}
    return {
        "account_id": auth_claim.get("chatgpt_account_id"),
        "plan_tier": auth_claim.get("chatgpt_plan_type"),
        "account_email": claims.get("email"),
    }


# --- Credential storage ------------------------------------------------------


def save_credential(session: Session, user_id: int, token_response: dict) -> None:
    """Persist (encrypted) the tokens from a successful exchange or refresh."""
    from app.models import ChatGptCredential

    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    if not access_token:
        raise DeviceCodeError("Token response contained no access_token.")

    info = account_info(token_response.get("id_token"))
    expires_in = int(token_response.get("expires_in") or 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    row = session.exec(
        select(ChatGptCredential).where(ChatGptCredential.user_id == user_id)
    ).first()

    if row is None:
        row = ChatGptCredential(user_id=user_id, access_token_encrypted="", refresh_token_encrypted="")
        session.add(row)

    row.access_token_encrypted = _encrypt(access_token)
    if refresh_token:
        row.refresh_token_encrypted = _encrypt(refresh_token)
    row.expires_at = expires_at
    row.last_refreshed_at = datetime.now(timezone.utc)
    row.disconnected_reason = None
    if info.get("account_id"):
        row.account_id = info["account_id"]
    if info.get("plan_tier"):
        row.plan_tier = info["plan_tier"]
    if info.get("account_email"):
        row.account_email = info["account_email"]

    session.commit()


def import_credential(session: Session, user_id: int, blob: dict) -> dict:
    """Store a credential the user obtained with OpenAI's own Codex CLI.

    The escape hatch for hosts whose egress IP ``auth.openai.com`` refuses (see
    ``AuthHostBlocked``): the user runs ``codex login`` on their own machine —
    the officially supported flow, on their own IP — and hands us the resulting
    ``~/.codex/auth.json``.

    Accepts either the whole auth.json (``{"tokens": {...}}``) or a bare
    ``{"access_token": ..., "refresh_token": ..., "id_token": ...}``.

    Inference then runs against chatgpt.com, which is reachable; only a later
    refresh needs the auth host again.
    """
    tokens = blob.get("tokens") if isinstance(blob.get("tokens"), dict) else blob
    access_token = tokens.get("access_token")
    if not access_token:
        raise DeviceCodeError(
            "No access_token found. Paste the contents of ~/.codex/auth.json "
            "from the machine where you ran `codex login`."
        )

    payload = {
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token"),
        # auth.json carries no expires_in. Codex access tokens are short-lived;
        # assume an hour so the first refresh attempt happens promptly rather
        # than after a surprise mid-turn 401.
        "expires_in": int(tokens.get("expires_in") or 3600),
    }
    save_credential(session, user_id, payload)

    row = get_credential(session, user_id)
    if row is not None and not row.account_id and tokens.get("account_id"):
        row.account_id = tokens["account_id"]
        session.commit()

    return status_for_user(session, user_id)


def get_credential(session: Session, user_id: int):
    """The user's credential row, or None."""
    from app.models import ChatGptCredential

    return session.exec(
        select(ChatGptCredential).where(ChatGptCredential.user_id == user_id)
    ).first()


def mark_disconnected(session: Session, user_id: int, reason: str) -> None:
    """Flag a credential dead so get_llm stops retrying it every turn."""
    row = get_credential(session, user_id)
    if row is None:
        return
    row.disconnected_reason = reason[:255]
    session.commit()


def delete_credential(session: Session, user_id: int) -> bool:
    """Disconnect: forget the tokens entirely."""
    row = get_credential(session, user_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def _how_connected(user_id: int, row) -> tuple:
    """(auth_method, can_auto_refresh) — derived, not stored.

    A pasted credential and a device-code one are indistinguishable once
    encrypted, but only one of them renews itself. The difference is whether the
    Codex CLI holds a login for this user: it is the component that does the
    refresh (see ``_refresh_payload``). Told nothing, a user on a pasted token
    gets silently dropped to the default model when it expires, mid-task.
    """
    from app.services import codex_cli_auth

    try:
        if codex_cli_auth.login_finished(user_id):
            return "device_code", True
        if not codex_cli_auth.cli_available():
            # Legacy image: the credential came from the direct device flow or a
            # paste, and either way refresh goes over our own HTTP call.
            return "http", bool(row.refresh_token_encrypted)
    except OSError:
        # Status is on the page-load path; an unreadable state dir must not 500.
        logger.warning("Could not read Codex CLI state for user %s", user_id)
        return "unknown", bool(row.refresh_token_encrypted)
    return "pasted_token", False


def status_for_user(session: Session, user_id: int) -> dict:
    """Non-secret connection status for the UI. Never returns token material."""
    row = get_credential(session, user_id)
    if row is None:
        return {"connected": False, "auth_method": None, "can_auto_refresh": False}

    expires_in = None
    if row.expires_at is not None:
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expires_in = int((expires_at - datetime.now(timezone.utc)).total_seconds())

    auth_method, can_auto_refresh = _how_connected(user_id, row)
    return {
        "connected": row.disconnected_reason is None,
        "account_email": row.account_email,
        "plan_tier": row.plan_tier,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "expires_in_seconds": expires_in,
        "auth_method": auth_method,
        "can_auto_refresh": can_auto_refresh,
        "disconnected_reason": row.disconnected_reason,
    }


def _needs_refresh(row) -> bool:
    if row.expires_at is None:
        return True
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) + _REFRESH_SKEW >= expires_at


def _run_coro_blocking(make_coro, timeout: float):
    """Run an async helper from ``get_llm``, which is called on both sync and
    async agent paths. Never leaves a loop running."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coro())

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(make_coro())).result(timeout=timeout)


def _refresh_payload(user_id: int, row) -> Optional[dict]:
    """Get a fresh token, preferring the Codex CLI over our own HTTP call.

    The CLI is tried FIRST because it is the only client that reliably reaches
    ``auth.openai.com`` — our httpx call is challenged by Cloudflare (see
    ``codex_cli_auth``). The direct HTTP path stays as the fallback for images
    without the CLI, and for hosts where our client is not challenged.
    """
    from app.services import codex_cli_auth

    if codex_cli_auth.cli_available() and codex_cli_auth.login_finished(user_id):
        blob = _run_coro_blocking(
            lambda: codex_cli_auth.refresh_via_cli(user_id), timeout=90
        )
        tokens = (blob or {}).get("tokens") or {}
        if tokens.get("access_token"):
            return {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token"),
                "id_token": tokens.get("id_token"),
                "expires_in": int(tokens.get("expires_in") or 3600),
            }
        logger.warning("Codex CLI could not refresh user %s; trying direct HTTP.", user_id)

    refresh_token = _decrypt(row.refresh_token_encrypted)
    if not refresh_token:
        return None
    return _run_coro_blocking(
        lambda: refresh_access_token(refresh_token), timeout=_HTTP_TIMEOUT + 5
    )


def access_token_for_user_sync(user_id: int) -> Optional[tuple]:
    """Return ``(access_token, account_id)`` for this user, refreshing if needed.

    Returns None when the user has no credential, it is marked disconnected, it
    cannot be decrypted, or a refresh fails — the caller (``get_llm``) must then
    fail open to the operator's default model. Synchronous because ``get_llm`` is
    called from both sync and async agent paths.

    Never raises: a broken credential must degrade to the default model, never
    break the chat turn.
    """
    try:
        from app.db import engine
        if engine is None:
            return None
        with Session(engine) as session:
            row = get_credential(session, user_id)
            if row is None or row.disconnected_reason:
                return None

            if not _needs_refresh(row):
                token = _decrypt(row.access_token_encrypted)
                return (token, row.account_id) if token else None

            payload = _refresh_payload(user_id, row)
            if payload is None:
                return None

            save_credential(session, user_id, payload)
            row = get_credential(session, user_id)
            token = _decrypt(row.access_token_encrypted)
            return (token, row.account_id) if token else None

    except DeviceCodeError as e:
        logger.warning("ChatGPT credential refresh rejected for user %s: %s", user_id, e)
        try:
            from app.db import engine
            with Session(engine) as session:
                mark_disconnected(session, user_id, str(e))
        except Exception:
            pass
        return None
    except Exception as e:
        logger.warning("ChatGPT credential unavailable for user %s: %s", user_id, e)
        return None


def new_state_token() -> str:
    """Opaque handle for an in-flight device-code login."""
    return secrets.token_urlsafe(24)
