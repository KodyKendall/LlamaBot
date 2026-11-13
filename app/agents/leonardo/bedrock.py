"""AWS Bedrock provider (LP-29, 0.7.20).

For institutional boxes whose inference must stay inside their own AWS account
(first: University of Utah, us-west-2). Today: Claude Haiku 5.5 only.

**GPT-6 Luna is not here on purpose.** OpenAI's models on Bedrock are the
open-weight gpt-oss family; there is no Luna to register, and the ticket forbids
substituting one.

**Credentials: the instance role, nothing else.** Every credential source except
the ECS task role and the EC2 instance profile is removed from the botocore
session, so static ``AWS_ACCESS_KEY_ID`` / shared config files / SSO are never
used even if present, and IMDSv2 is forced. A box that has static keys in its env
is reported unavailable, loudly, so a misconfiguration is noticed rather than
quietly ignored.

**Opt-in per box.** Bedrock is offered only when ``BEDROCK_REGION`` is set. Every
other box (the whole Hetzner fleet) answers "unavailable" without touching boto3
or the metadata endpoint, so it costs nothing at startup or per request.

**Model id.** Not verifiable from the dev box (no AWS access there). The default
is a best guess at the regional on-demand id; ``BEDROCK_HAIKU_5_5_MODEL_ID``
overrides it — set it from ``aws bedrock list-foundation-models --region
us-west-2`` on the target box (an inference-profile id such as ``us.anthropic...``
also works if on-demand is not offered). See docs/dev/bedrock_provider.md.

boto3 / langchain-aws are imported lazily: a box without them still boots.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

REGION_ENV = "BEDROCK_REGION"

#: frontend name -> how to reach it on Bedrock.
MODELS = {
    "claude-haiku-5.5-bedrock": {
        "id_env": "BEDROCK_HAIKU_5_5_MODEL_ID",
        # UNVERIFIED regional on-demand id — see the module docstring.
        "default_id": "anthropic.claude-haiku-5-5-v1:0",
        "provider": "anthropic",
        # Haiku 5.5 is adaptive-thinking only (a thinking budget is a 400).
        "request_fields": {"thinking": {"type": "adaptive"}},
    },
}

#: The only botocore credential providers kept: ECS task role, EC2 instance role.
ROLE_PROVIDERS = ("container-role", "iam-role")
_STATIC_KEY_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")

_CACHE_SECONDS = 300
_cache_lock = threading.Lock()
_cached = None  # (checked_at, bool)


def is_bedrock_model(model_name: str) -> bool:
    return model_name in MODELS


def region() -> str:
    return (os.getenv(REGION_ENV) or "").strip()


def model_id(model_name: str) -> str:
    entry = MODELS[model_name]
    return (os.getenv(entry["id_env"]) or "").strip() or entry["default_id"]


def static_keys_present() -> bool:
    return any((os.getenv(v) or "").strip() for v in _STATIC_KEY_VARS)


def role_only_session():
    """A botocore session that can only get credentials from the instance role."""
    import botocore.session

    session = botocore.session.Session()
    resolver = session.get_component("credential_provider")
    for provider in list(resolver.providers):
        if provider.METHOD not in ROLE_PROVIDERS:
            resolver.remove(provider.METHOD)
    session.set_config_variable("ec2_metadata_v1_disabled", True)  # IMDSv2 only
    # Off AWS the metadata endpoint never answers; don't make anyone wait long.
    session.set_config_variable("metadata_service_timeout", 1)
    session.set_config_variable("metadata_service_num_attempts", 1)
    return session


def _role_credentials():
    return role_only_session().get_credentials()


def available() -> bool:
    """True when this box opted in AND can get instance-role credentials. Never raises."""
    global _cached
    if not region():
        return False
    if static_keys_present():
        logger.error(
            "Bedrock: static AWS keys are set in the environment; they are not "
            "allowed. Remove them and use the instance role. Bedrock is disabled."
        )
        return False
    with _cache_lock:
        now = time.monotonic()
        if _cached and now - _cached[0] < _CACHE_SECONDS:
            return _cached[1]
        try:
            ok = _role_credentials() is not None
        except Exception as e:  # noqa: BLE001 - missing boto3, IMDS down, anything
            logger.warning("Bedrock unavailable: %s", e)
            ok = False
        if not ok:
            logger.warning("Bedrock unavailable: no instance-role credentials on this box.")
        _cached = (now, ok)
        return ok


def reset_cache() -> None:
    global _cached
    with _cache_lock:
        _cached = None


def build_client(model_name: str):
    """ChatBedrockConverse for ``model_name``. Call via llm_factory.get_llm, never directly.

    Construction never contacts AWS; a box that cannot authenticate fails on the
    first call, into the normal resilience path, like any provider with a bad key.
    """
    import boto3
    from botocore.config import Config
    from langchain_aws import ChatBedrockConverse

    entry = MODELS[model_name]
    region_name = region() or "us-west-2"
    runtime = boto3.Session(
        botocore_session=role_only_session(), region_name=region_name,
    ).client(
        "bedrock-runtime",
        config=Config(
            read_timeout=180, connect_timeout=10,
            # Retries belong to the agent middleware, as for every other provider.
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    return ChatBedrockConverse(
        client=runtime,
        model_id=model_id(model_name),
        provider=entry["provider"],
        region_name=region_name,
        max_tokens=16384,
        additional_model_request_fields=entry["request_fields"],
    )
