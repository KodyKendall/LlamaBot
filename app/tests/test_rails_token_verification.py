"""Round-trip verification of real ``llama_bot_rails`` WebSocket tokens.

The bug: ``verify_rails_token`` verified nothing. It accepted any string
containing ``--`` as proof of a trusted internal caller, so ``{"api_token":
"x--y"}`` authenticated as the Rails gem from anywhere on the internet, and
the agent-mode gate skipped ``rails_auth`` callers entirely.

Every token below was minted by a **real Rails 7.2.3 app** with the gem loaded,
using the same digest and serializer ``Rails.application.message_verifier(
:llamabot_ws)`` uses, against the throwaway secret in ``TEST_SKB``. They are
golden vectors, not fixtures our own code produced — that is the whole point:
they prove the Python verifier agrees with Ruby, rather than with itself.

Run with: pytest app/tests/test_rails_token_verification.py -v
"""
import pytest

from app.services.rails_message_verifier import (
    InvalidRailsToken,
    derive_key,
    verify,
)

from app.tests.rails_token_vectors import (  # noqa: F401
    EXPIRED,
    JSON_SERIALIZER,
    NO_METADATA,
    OTHER_SKB,
    SHA256_DIGEST,
    SHA256_KEY_DIGEST,
    TAMPERED,
    TEST_SKB,
    VALID,
    VALID_NO_USER,
    WRONG_KEY,
)


class TestAcceptsRealTokens:
    """Criterion 1: a token Rails actually minted verifies, with the right user."""

    def test_verifies_and_extracts_user_id(self):
        payload = verify(VALID, TEST_SKB)
        assert payload["user_id"] == 4242
        assert payload["session_id"] == "sess-1"

    def test_signed_out_rails_app_still_verifies(self):
        # current_user_resolver returns nil when nobody is signed into the Rails
        # app, and the gem mints `user_id: nil` rather than refusing. The
        # signature still proves the box's own Rails app sent it, so this must
        # keep working or every signed-out box loses its embedded chat.
        payload = verify(VALID_NO_USER, TEST_SKB)
        assert payload["user_id"] is None
        assert payload["session_id"] == "sess-2"

    def test_payload_without_metadata_envelope(self):
        # Rails only wraps in `_rails` when expiry or purpose is set.
        payload = verify(NO_METADATA, TEST_SKB)
        assert payload["user_id"] == 55

    def test_json_serialized_payload(self):
        # A box with `message_serializer = :json` mints JSON, not Marshal.
        payload = verify(JSON_SERIALIZER, TEST_SKB)
        assert payload["user_id"] == 606

    def test_stronger_signing_digest_still_verifies(self):
        payload = verify(SHA256_DIGEST, TEST_SKB)
        assert payload["user_id"] == 777

    def test_stronger_key_derivation_digest_still_verifies(self):
        # `key_generator_hash_digest_class` is a separate setting from the
        # signing digest, and a box may have either. Assuming one cost us a
        # rejected live token — see rails_token_vectors.
        payload = verify(SHA256_KEY_DIGEST, TEST_SKB)
        assert payload["user_id"] == 888


class TestRejectsForgeries:
    """Criterion 2: the forgeries that used to sail through."""

    @pytest.mark.parametrize("token", [
        "x--y",              # the exact string from the disclosure
        "--",
        "a--b--c",
        "",
        "no-separator",
        "eyJhbGciOiJIUzI1NiJ9.body.sig",
    ])
    def test_junk_is_rejected(self, token):
        with pytest.raises(InvalidRailsToken):
            verify(token, TEST_SKB)

    def test_token_signed_with_a_different_key_is_rejected(self):
        with pytest.raises(InvalidRailsToken):
            verify(WRONG_KEY, TEST_SKB)

    def test_expired_token_is_rejected(self):
        with pytest.raises(InvalidRailsToken):
            verify(EXPIRED, TEST_SKB)

    def test_tampered_payload_is_rejected(self):
        with pytest.raises(InvalidRailsToken):
            verify(TAMPERED, TEST_SKB)

    def test_valid_signature_wrong_salt_is_rejected(self):
        with pytest.raises(InvalidRailsToken):
            verify(VALID, TEST_SKB, salt="some_other_verifier")

    def test_purpose_mismatch_is_rejected(self):
        # These tokens carry no purpose; demanding one must fail closed.
        with pytest.raises(InvalidRailsToken):
            verify(VALID, TEST_SKB, purpose="login")


class TestKeyDerivation:
    """The derivation digest (SHA256) is not the signing digest (SHA1)."""

    def test_key_is_64_bytes(self):
        assert len(derive_key(TEST_SKB)) == 64

    def test_salt_changes_the_key(self):
        assert derive_key(TEST_SKB, "llamabot_ws") != derive_key(TEST_SKB, "other")

    def test_secret_changes_the_key(self):
        assert derive_key(TEST_SKB) != derive_key(OTHER_SKB)


class TestTokenServiceIntegration:
    """The seam the WebSocket actually calls."""

    @pytest.fixture(autouse=True)
    def _secret(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY_BASE", TEST_SKB)

    def test_forged_token_is_refused(self):
        from app.services.token_service import verify_rails_token
        assert verify_rails_token("x--y") is None

    def test_real_token_is_accepted_as_rails_auth(self):
        from app.services.token_service import verify_rails_token
        payload = verify_rails_token(VALID)
        assert payload is not None
        assert payload["type"] == "rails_auth"
        assert payload["rails_user_id"] == 4242

    def test_rails_user_id_is_not_exposed_as_user_id(self):
        # `user_id` is a LlamaBot auth-DB id and gets stamped as the turn owner
        # (request_context.set_current_user_id) to pick whose ChatGPT
        # subscription to spend. A Rails app's user id lives in a different
        # namespace entirely — leaking it into that key would bill the wrong
        # account, so it travels under `rails_user_id`.
        from app.services.token_service import verify_rails_token
        assert verify_rails_token(VALID).get("user_id") is None

    def test_expired_token_is_refused(self):
        from app.services.token_service import verify_rails_token
        assert verify_rails_token(EXPIRED) is None

    def test_foreign_key_token_is_refused(self):
        from app.services.token_service import verify_rails_token
        assert verify_rails_token(WRONG_KEY) is None

    def test_without_a_shared_secret_nothing_is_trusted(self, monkeypatch):
        # No SECRET_KEY_BASE means no way to tell real from forged. Fail closed.
        monkeypatch.delenv("SECRET_KEY_BASE", raising=False)
        from app.services.token_service import verify_rails_token
        assert verify_rails_token(VALID) is None

    def test_json_serialized_rails_token_is_recognised(self):
        # It starts with "eyJ", which the old shape check used as its "this is a
        # JWT, not a Rails token" signal — so these were misrouted and refused.
        from app.services.token_service import is_rails_token
        assert is_rails_token(JSON_SERIALIZER) is True

    def test_a_real_jwt_is_not_mistaken_for_a_rails_token(self):
        from app.services.token_service import is_rails_token
        assert is_rails_token("eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.sig") is False
