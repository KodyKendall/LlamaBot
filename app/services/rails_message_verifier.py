"""Verify ``ActiveSupport::MessageVerifier`` tokens minted by ``llama_bot_rails``.

The gem hands the browser a token for the chat WebSocket::

    Rails.application.message_verifier(:llamabot_ws)
         .generate({session_id:, user_id:}, expires_in: 30.minutes)

and every frame from the Rails-embedded chat carries a freshly minted one
(``app/channels/llama_bot_rails/chat_channel.rb`` mints per ``receive``).

Before this module existed LlamaBot did not check the signature at all — it
accepted any string containing ``--`` as proof of a trusted internal caller,
which made the whole agent-authorization gate forgeable from the public
internet. Verification happens locally: on a Leo box the llamabot and Rails
containers read the same ``SECRET_KEY_BASE``, so there is no HTTP round trip
to Rails per connection.

Wire format (all of it confirmed against a live Rails 7.2.3 box, not read off
the docs — see ``app/tests/test_rails_token_verification.py`` for the golden
vectors this was built from)::

    <base64(serialized payload)>--<hex HMAC of that base64 text>

  * **Key** — ``PBKDF2-HMAC(secret_key_base, "llamabot_ws", 1000, 64)``, whose
    digest is Rails' ``key_generator_hash_digest_class``. A stock Rails 7.2 box
    uses **SHA1** here even on ``load_defaults 7.2`` — measured, not assumed;
    an app that has set it to SHA256 is equally valid, so both are tried.
  * **Signature** — HMAC over the base64 *text*, using ``MessageVerifier``'s own
    digest, which defaults to SHA1 and is a *separate* setting from the
    derivation digest above. Confusing the two is the easy mistake here, and it
    fails in the most misleading way possible: a self-minted test vector still
    round-trips, because it is wrong in both directions at once. Only a token
    from a real Rails app catches it.
  * **Payload** — Marshal on a stock box, JSON where the app has opted into
    ``message_serializer = :json``. Rails' own ``*WithFallback`` serializers
    read either, so this does too.
  * **Metadata** — Rails 7.1+ wraps the payload as
    ``{"_rails" => {"data" => …, "exp" => …, "pur" => …}}`` when any of
    expiry/purpose is set, and leaves it bare when none is.

Marshal parsing runs **only after** the HMAC checks out, so the bytes handed to
it are already proven to come from the box's own Rails app. The reader is still
a strict allowlist that raises on any type it doesn't know — it never
constructs objects, so a Marshal payload cannot become code execution.
"""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Rails' KeyGenerator defaults, and the salt the gem names its verifier with.
KEY_SALT = "llamabot_ws"
KEY_ITERATIONS = 1000
KEY_LENGTH = 64

# Rails' `key_generator_hash_digest_class`. SHA1 is what a stock box actually
# uses; SHA256 covers an app that has opted into it. Offering both costs two
# cheap PBKDF2 runs (cached) and gives an attacker nothing — every candidate
# still needs the secret.
KEY_DIGESTS = ("sha1", "sha256")

# MessageVerifier's message digest defaults to SHA1. A box that has rotated to a
# stronger one still verifies: every candidate needs the same HMAC key, so
# offering more than one costs nothing an attacker can use.
CANDIDATE_DIGESTS = ("sha1", "sha256", "sha512")


class InvalidRailsToken(Exception):
    """Token failed signature, structure, or expiry checks."""


# --------------------------------------------------------------------------
# Ruby Marshal (format 4.8) — the small, strict subset a token payload uses.
# --------------------------------------------------------------------------

class _MarshalReader:
    """Reads the handful of Marshal types a MessageVerifier payload can hold.

    Supports nil/true/false, integers, symbols, strings, arrays and hashes,
    plus the symbol- and object-link back-references Ruby emits for repeats.
    Anything else — user-defined types, ``Object`` dumps — raises. A token
    payload is a flat hash of scalars, so a wider reader would only add ways
    to be wrong.
    """

    def __init__(self, data: bytes):
        self.d = data
        self.i = 0
        self.symbols: list = []
        self.objects: list = []

    def _byte(self) -> int:
        if self.i >= len(self.d):
            raise InvalidRailsToken("truncated marshal payload")
        b = self.d[self.i]
        self.i += 1
        return b

    def _take(self, n: int) -> bytes:
        if n < 0 or self.i + n > len(self.d):
            raise InvalidRailsToken("truncated marshal payload")
        s = self.d[self.i:self.i + n]
        self.i += n
        return s

    def _long(self) -> int:
        """Ruby's packed variable-length integer."""
        c = self._byte()
        if c > 127:
            c -= 256
        if c == 0:
            return 0
        if c > 0:
            if c > 4:
                return c - 5
            val = 0
            for k in range(c):
                val |= self._byte() << (8 * k)
            return val
        if c < -4:
            return c + 5
        val = -1
        for k in range(-c):
            val &= ~(0xFF << (8 * k))
            val |= self._byte() << (8 * k)
        return val

    def read(self) -> Any:
        t = self._byte()
        if t == 0x30:            # '0' nil
            return None
        if t == 0x54:            # 'T'
            return True
        if t == 0x46:            # 'F'
            return False
        if t == 0x69:            # 'i' integer
            return self._long()
        if t == 0x3A:            # ':' symbol
            sym = self._take(self._long()).decode("utf-8", "replace")
            self.symbols.append(sym)
            return sym
        if t == 0x3B:            # ';' symbol back-reference
            idx = self._long()
            if not 0 <= idx < len(self.symbols):
                raise InvalidRailsToken("bad symbol link")
            return self.symbols[idx]
        if t == 0x22:            # '"' string
            s = self._take(self._long()).decode("utf-8", "replace")
            self.objects.append(s)
            return s
        if t == 0x49:            # 'I' object carrying instance variables
            inner = self.read()
            for _ in range(self._long()):
                self.read()      # ivar name (e.g. :E, the encoding flag)
                self.read()      # ivar value — encoding is not data we need
            return inner
        if t == 0x5B:            # '[' array
            n = self._long()
            arr: list = []
            self.objects.append(arr)
            for _ in range(n):
                arr.append(self.read())
            return arr
        if t == 0x7B:            # '{' hash
            n = self._long()
            h: dict = {}
            self.objects.append(h)
            for _ in range(n):
                k = self.read()
                h[k] = self.read()
            return h
        if t == 0x40:            # '@' object back-reference
            idx = self._long()
            if not 0 <= idx < len(self.objects):
                raise InvalidRailsToken("bad object link")
            return self.objects[idx]
        raise InvalidRailsToken(f"unsupported marshal type {chr(t)!r}")


def _marshal_load(blob: bytes) -> Any:
    if len(blob) < 2 or blob[0] != 0x04:
        raise InvalidRailsToken("not a marshal payload")
    return _MarshalReader(blob[2:]).read()


def _deserialize(blob: bytes) -> Any:
    """JSON if it parses, else Marshal — mirroring Rails' *WithFallback pair."""
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _marshal_load(blob)


# --------------------------------------------------------------------------
# Signature + metadata
# --------------------------------------------------------------------------

@lru_cache(maxsize=16)
def derive_key(secret_key_base: str, salt: str = KEY_SALT,
               key_digest: str = "sha1") -> bytes:
    """Rails' ``KeyGenerator`` key for a named verifier.

    Cached because the gem mints a fresh token on *every* frame, so this would
    otherwise run PBKDF2 twice per chat message.
    """
    return hashlib.pbkdf2_hmac(
        key_digest, secret_key_base.encode("utf-8"), salt.encode("utf-8"),
        KEY_ITERATIONS, KEY_LENGTH,
    )


def _signature_ok(encoded: str, signature: str,
                  secret_key_base: str, salt: str) -> bool:
    message = encoded.encode("utf-8")
    matched = False
    for key_digest in KEY_DIGESTS:
        key = derive_key(secret_key_base, salt, key_digest)
        for digest in CANDIDATE_DIGESTS:
            expected = hmac.new(key, message, digest).hexdigest()
            # No early return: comparing every candidate keeps the work done
            # constant whether or not an early one matched.
            if hmac.compare_digest(expected, signature):
                matched = True
    return matched


def _unwrap_metadata(payload: Any, purpose: Optional[str]) -> Any:
    """Strip Rails' ``_rails`` envelope, enforcing expiry and purpose.

    A payload minted with neither expiry nor purpose is stored bare, so an
    envelope-less payload is legitimate — it simply never expires.
    """
    if not isinstance(payload, dict) or "_rails" not in payload:
        if purpose is not None:
            raise InvalidRailsToken("token carries no purpose")
        return payload

    meta = payload["_rails"]
    if not isinstance(meta, dict):
        raise InvalidRailsToken("malformed metadata envelope")

    if meta.get("pur") != purpose:
        raise InvalidRailsToken("purpose mismatch")

    exp = meta.get("exp")
    if exp is not None:
        if not isinstance(exp, str):
            raise InvalidRailsToken("malformed expiry")
        try:
            expires_at = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except ValueError:
            raise InvalidRailsToken("unparseable expiry")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise InvalidRailsToken("token expired")

    if "data" in meta:
        return meta["data"]
    if "message" in meta:
        # Pre-7.1 envelope: the real payload is base64 inside the metadata.
        try:
            inner = base64.b64decode(meta["message"])
        except (binascii.Error, ValueError):
            raise InvalidRailsToken("malformed legacy envelope")
        return _deserialize(inner)
    raise InvalidRailsToken("metadata envelope carries no payload")


def verify(token: str, secret_key_base: str, *,
           salt: str = KEY_SALT, purpose: Optional[str] = None) -> Any:
    """Return the payload a Rails MessageVerifier token carries, or raise.

    Raises :class:`InvalidRailsToken` for anything that does not verify — a bad
    signature, a foreign key, an expired token, a malformed payload.
    """
    if not token or not isinstance(token, str):
        raise InvalidRailsToken("empty token")

    parts = token.split("--")
    if len(parts) != 2:
        raise InvalidRailsToken("token is not <payload>--<signature>")
    encoded, signature = parts
    if not encoded or not signature:
        raise InvalidRailsToken("token is not <payload>--<signature>")

    if not _signature_ok(encoded, signature, secret_key_base, salt):
        raise InvalidRailsToken("signature mismatch")

    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise InvalidRailsToken("payload is not base64")

    return _unwrap_metadata(_deserialize(blob), purpose)


def secret_key_base() -> Optional[str]:
    """The shared secret, or None if this deployment never supplied one.

    Both containers on a Leo box read the same ``.env``. If it is missing there
    is no way to tell a real Rails token from a forged one, so callers must
    treat None as "reject", never as "trust".
    """
    value = os.getenv("SECRET_KEY_BASE")
    return value if value else None
