"""Magic-link token verification.

Cross-language token format shared with the LlamaPress.ai Rails mothership:

    base64url(json({"username": <str>, "exp": <unix_seconds>}))
        + "." +
    base64url(hmac_sha256(LLAMAPRESS_AI_LOGIN_SECRET, payload_b64))

Both Python and Ruby produce identical bytes using only stdlib. We do NOT
generate magic-link tokens here — Rails generates them, we only verify.

The shared secret (LLAMAPRESS_AI_LOGIN_SECRET) is provisioned once on the
Rails host and passed into every spawned LlamaBot via Leonardo's existing
env templating. It is intentionally distinct from SESSION_SECRET so a leak
of one cannot impersonate the other, and they can rotate independently.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

MAGIC_LINK_SECRET_ENV = "LLAMAPRESS_AI_LOGIN_SECRET"

# 30 seconds of clock skew tolerance for the exp claim (standard JWT practice).
CLOCK_SKEW_SECONDS = 30


class MagicLinkError(Exception):
    """Base class for magic-link verification failures."""


class MagicLinkSecretMissing(MagicLinkError):
    """Raised when LLAMAPRESS_AI_LOGIN_SECRET is not configured."""


class MagicLinkInvalid(MagicLinkError):
    """Raised when the token is malformed, tampered, or expired."""


def _b64url_decode(s: str) -> bytes:
    # Re-pad to a multiple of 4 — base64.urlsafe_b64decode requires padding.
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def verify_magic_link_token(token: str) -> str:
    """Verify a Rails-generated magic-link token. Returns the username on success.

    Raises:
        MagicLinkSecretMissing: LLAMAPRESS_AI_LOGIN_SECRET env var is unset.
            Caller should map this to a 503 with a clear operator-facing message.
        MagicLinkInvalid: malformed, bad signature, expired, or wrong shape.
            Caller should map this to a 401.
    """
    secret = os.getenv(MAGIC_LINK_SECRET_ENV)
    if not secret:
        raise MagicLinkSecretMissing(
            f"{MAGIC_LINK_SECRET_ENV} is not configured on this LlamaBot instance"
        )

    if not token or token.count(".") != 1:
        raise MagicLinkInvalid("malformed token")

    payload_b64, sig_b64 = token.split(".", 1)

    # HMAC is computed over the base64url-encoded payload string (matches Ruby).
    expected_sig = hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    try:
        provided_sig = _b64url_decode(sig_b64)
    except Exception:
        raise MagicLinkInvalid("malformed signature")

    if not hmac.compare_digest(expected_sig, provided_sig):
        raise MagicLinkInvalid("bad signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        raise MagicLinkInvalid("malformed payload")

    if not isinstance(payload, dict):
        raise MagicLinkInvalid("payload not an object")

    username = payload.get("username")
    exp = payload.get("exp")
    if not isinstance(username, str) or not username:
        raise MagicLinkInvalid("missing username")
    if not isinstance(exp, int):
        raise MagicLinkInvalid("missing or non-integer exp")

    if time.time() > exp + CLOCK_SKEW_SECONDS:
        raise MagicLinkInvalid("token expired")

    return username
