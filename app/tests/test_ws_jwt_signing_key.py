"""The browser WebSocket JWT must never be signed with a publicly-known key.

The bug: token_service signed and verified ``ws_auth`` JWTs with
``WS_SECRET_KEY``, else ``SECRET_KEY``, else a hard-coded string that is in this
open-source repo. The provisioner never set either variable, and both compose
files set ``WS_SECRET_KEY=`` to an empty string, so every box signed with a key
anyone could read. The ``role``/``is_admin`` claims in the token are what the
agent-mode gate trusts, so a token minted offline cleared auth AND the gate.

The fix: a missing, empty or placeholder key is never used. The key falls back
to one derived from the box's own SESSION_SECRET (random per box, durable in the
auth DB), and stays distinct from it so a WS token still cannot mint a browser
session.

Run with: pytest app/tests/test_ws_jwt_signing_key.py -v
"""
import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

PUBLIC_FALLBACK = "fallback-dev-key-change-in-production"
BOX_SESSION_SECRET = "a1" * 32


@pytest.fixture
def token_service(monkeypatch):
    """Reload token_service under a patched env, and restore it afterwards."""
    import app.services.token_service as ts

    def load(**env):
        for name in ("WS_SECRET_KEY", "SECRET_KEY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SESSION_SECRET", BOX_SESSION_SECRET)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(ts)

    yield load
    monkeypatch.undo()
    importlib.reload(ts)


def _forged(key, **claims):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "attacker",
        "user_id": 1,
        "role": "admin",
        "is_admin": True,
        "type": "ws_auth",
        "iat": now,
        "exp": now + timedelta(minutes=30),
        **claims,
    }
    return jwt.encode(payload, key, algorithm="HS256")


def _user(**attrs):
    user = MagicMock()
    user.id = attrs.get("id", 7)
    user.username = attrs.get("username", "alice")
    user.role = attrs.get("role", "engineer")
    user.is_admin = attrs.get("is_admin", False)
    return user


# --- the hole ---------------------------------------------------------------

def test_a_token_signed_with_the_public_fallback_is_rejected(token_service):
    ts = token_service()  # nothing configured, like every provisioned box
    assert ts.verify_ws_token(_forged(PUBLIC_FALLBACK)) is None


def test_an_empty_ws_secret_key_is_not_used_as_the_key(token_service):
    """Both compose files ship `WS_SECRET_KEY=` — set, but empty."""
    ts = token_service(WS_SECRET_KEY="")
    assert ts.SECRET_KEY
    assert ts.verify_ws_token(_forged(PUBLIC_FALLBACK)) is None


def test_the_placeholder_set_explicitly_is_not_trusted_either(token_service):
    ts = token_service(WS_SECRET_KEY=PUBLIC_FALLBACK)
    assert ts.SECRET_KEY != PUBLIC_FALLBACK
    assert ts.verify_ws_token(_forged(PUBLIC_FALLBACK)) is None


@pytest.mark.asyncio
async def test_a_forged_admin_token_does_not_authenticate_the_socket(token_service):
    """The claims in a forged token never reach the agent-mode gate."""
    token_service()
    from app.websocket.web_socket_handler import WebSocketHandler

    manager = MagicMock()
    manager.send_personal_message = AsyncMock()
    with patch("app.websocket.request_handler.RequestHandler"):
        handler = WebSocketHandler(MagicMock(), manager)

    ok = await handler._handle_auth_message({"token": _forged(PUBLIC_FALLBACK)})

    assert ok is False
    assert handler.authenticated is False
    assert manager.send_personal_message.await_args[0][0]["type"] == "auth_error"


# --- the normal browser path still works --------------------------------------

def test_an_unconfigured_box_still_mints_and_verifies_its_own_tokens(token_service):
    ts = token_service()
    payload = ts.verify_ws_token(ts.create_ws_token(_user(role="engineer")))
    assert payload["sub"] == "alice"
    assert payload["role"] == "engineer"


def test_the_derived_key_is_stable_across_restarts(token_service):
    """A token minted before a recreate must verify after it (same auth DB)."""
    token = token_service().create_ws_token(_user())
    assert token_service().verify_ws_token(token) is not None


def test_the_derived_key_is_not_the_session_secret(token_service):
    """A leaked WS token must not be able to mint browser sessions."""
    ts = token_service()
    assert ts.SECRET_KEY != ts.SESSION_SECRET
    assert ts.verify_session_token(ts.create_ws_token(_user())) is None


def test_a_real_configured_key_is_used_as_is(token_service):
    ts = token_service(WS_SECRET_KEY="operator-chosen-" + "k" * 32)
    assert ts.SECRET_KEY == "operator-chosen-" + "k" * 32
    assert ts.verify_ws_token(ts.create_ws_token(_user())) is not None
