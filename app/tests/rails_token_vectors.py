"""Golden ``llama_bot_rails`` WebSocket tokens, minted by a real Rails app.

Not fixtures our own code produced — these came out of Rails 7.2.3 with the
gem loaded, reproducing exactly how ``Rails.application.message_verifier(
:llamabot_ws)`` builds its verifier, signed with the throwaway secret in
:data:`TEST_SKB`. That is what makes them worth anything: they prove the Python
verifier agrees with Ruby rather than with itself.

The measured parameters on a stock box, none of which are safe to assume:

  * signing digest      SHA1
  * key_generator digest OpenSSL::Digest::SHA1  ← *not* the same setting
  * serializer          MarshalWithFallback

The first cut of this file used SHA256 for the key derivation, on the strength
of a written description. Every test passed, because the vectors had been
minted under the same wrong assumption — and a token from the live Rails app
was rejected. Regenerate these against a real container; never hand-edit them,
and never mint them from Python.
"""

TEST_SKB = "llamabot-test-secret-key-base-do-not-use-in-production-0123456789"
OTHER_SKB = "a-completely-different-secret-key-base-9876543210"

VALID = "BAh7BkkiC19yYWlscwY6BkVUewdJIglkYXRhBjsAVHsHOg9zZXNzaW9uX2lkSSILc2Vzcy0xBjsAVDoMdXNlcl9pZGkCkhBJIghleHAGOwBUSSIdMjA5OS0wMS0wMVQwMDowMDowMC4wMDBaBjsAVA==--7ee6f04c9464123fdf2d64a87fe24460abc3c2f2"
VALID_NO_USER = "BAh7BkkiC19yYWlscwY6BkVUewdJIglkYXRhBjsAVHsHOg9zZXNzaW9uX2lkSSILc2Vzcy0yBjsAVDoMdXNlcl9pZDBJIghleHAGOwBUSSIdMjA5OS0wMS0wMVQwMDowMDowMC4wMDBaBjsAVA==--3882566dab1098e28d1929d919b1d72fda377008"
EXPIRED = "BAh7BkkiC19yYWlscwY6BkVUewdJIglkYXRhBjsAVHsHOg9zZXNzaW9uX2lkSSILc2Vzcy0zBjsAVDoMdXNlcl9pZGkMSSIIZXhwBjsAVEkiHTIwMjYtMDgtMjVUMDA6MzU6NDYuNDk5WgY7AFQ=--96f3a9cf61e869380a3c504c30ca99da41af566d"
WRONG_KEY = "BAh7BkkiC19yYWlscwY6BkVUewdJIglkYXRhBjsAVHsHOg9zZXNzaW9uX2lkSSILc2Vzcy00BjsAVDoMdXNlcl9pZGkOSSIIZXhwBjsAVEkiHTIwOTktMDEtMDFUMDA6MDA6MDAuMDAwWgY7AFQ=--2a4f7f718fbbe1685e876a45cc8c8abf43d28a72"
NO_METADATA = "BAh7BzoPc2Vzc2lvbl9pZEkiC3Nlc3MtNQY6BkVUOgx1c2VyX2lkaTw=--33ad5a935594d0bec6633543ee378e3a56d47182"
JSON_SERIALIZER = "eyJfcmFpbHMiOnsiZGF0YSI6eyJzZXNzaW9uX2lkIjoic2Vzcy02IiwidXNlcl9pZCI6NjA2fSwiZXhwIjoiMjA5OS0wMS0wMVQwMDowMDowMC4wMDBaIn19--11fcb8d3fcfa855e9f97b458ff756aabcf995ad6"
SHA256_DIGEST = "BAh7BkkiC19yYWlscwY6BkVUewdJIglkYXRhBjsAVHsHOg9zZXNzaW9uX2lkSSILc2Vzcy03BjsAVDoMdXNlcl9pZGkCCQNJIghleHAGOwBUSSIdMjA5OS0wMS0wMVQwMDowMDowMC4wMDBaBjsAVA==--eb7f34b0ac0f4630195eafa8667f345bd7bc7c1d2c496fbb63e602572b26f0bc"
SHA256_KEY_DIGEST = "BAh7BkkiC19yYWlscwY6BkVUewdJIglkYXRhBjsAVHsHOg9zZXNzaW9uX2lkSSILc2Vzcy04BjsAVDoMdXNlcl9pZGkCeANJIghleHAGOwBUSSIdMjA5OS0wMS0wMVQwMDowMDowMC4wMDBaBjsAVA==--c300e6e463d9bf5da6d1e56bdebecf8807b87167"
TAMPERED = "CAh7BkkiC19yYWlscwY6BkVUewdJIglkYXRhBjsAVHsHOg9zZXNzaW9uX2lkSSILc2Vzcy0xBjsAVDoMdXNlcl9pZGkCkhBJIghleHAGOwBUSSIdMjA5OS0wMS0wMVQwMDowMDowMC4wMDBaBjsAVA==--7ee6f04c9464123fdf2d64a87fe24460abc3c2f2"
