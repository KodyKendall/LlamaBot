"""LP-29: Claude Haiku 5.5 on AWS Bedrock, authenticated by the instance role only.

For institutional boxes (University of Utah, us-west-2) whose inference must stay
inside their AWS account. Structure-only: no test here calls AWS.

GPT-6 Luna is deliberately NOT registered: OpenAI's models on Bedrock are the
open-weight gpt-oss family only, so there is no Luna to register (and the ticket
forbids substituting one).
"""
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.agents.leonardo import bedrock, llm_factory, zdr
from app.agents.leonardo.model_capabilities import MODEL_CAPABILITIES, ZDR_COMPLIANT
from app.agents.leonardo.model_policy import _KNOWN_MODELS

HAIKU = "claude-haiku-5.5-bedrock"
STATIC_KEY_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (bedrock.REGION_ENV, bedrock.MODELS[HAIKU]["id_env"], *STATIC_KEY_VARS,
                "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)
    bedrock.reset_cache()
    yield
    bedrock.reset_cache()


# ---------------------------------------------------------------- registry

def test_haiku_bedrock_is_registered_everywhere():
    assert HAIKU in MODEL_CAPABILITIES
    assert HAIKU in _KNOWN_MODELS
    assert f'model_name == "{HAIKU}"' in (
        __import__("inspect").getsource(llm_factory._build_client)
    )


def test_haiku_bedrock_reads_images_only():
    assert MODEL_CAPABILITIES[HAIKU] == {"images": True, "video": False, "pdf": False}


def test_luna_is_not_registered_on_bedrock():
    assert "gpt-6-luna-bedrock" not in MODEL_CAPABILITIES
    assert "gpt-6-luna-bedrock" not in _KNOWN_MODELS
    assert "gpt-6-luna-bedrock" not in bedrock.MODELS


def test_haiku_bedrock_is_not_the_fleet_default():
    assert llm_factory.DEFAULT_LLM_MODEL != HAIKU


# ---------------------------------------------------------------- client

def test_builds_a_converse_client_in_the_configured_region(monkeypatch):
    from langchain_aws import ChatBedrockConverse

    monkeypatch.setenv(bedrock.REGION_ENV, "us-west-2")
    client = llm_factory._build_client(HAIKU)

    assert isinstance(client, ChatBedrockConverse)
    assert client.model_id == bedrock.MODELS[HAIKU]["default_id"]
    assert client.region_name == "us-west-2"
    assert client.additional_model_request_fields == {"thinking": {"type": "adaptive"}}


def test_model_id_is_overridable(monkeypatch):
    monkeypatch.setenv(bedrock.REGION_ENV, "us-west-2")
    monkeypatch.setenv(bedrock.MODELS[HAIKU]["id_env"], "us.anthropic.claude-haiku-5-5-x:0")

    assert llm_factory._build_client(HAIKU).model_id == "us.anthropic.claude-haiku-5-5-x:0"


def test_default_id_is_regional_not_a_cross_region_profile():
    assert not bedrock.MODELS[HAIKU]["default_id"].startswith(("us.", "global.", "eu.", "apac."))


def test_no_anthropic_cache_blocks_go_to_bedrock():
    """cache_control is an Anthropic-direct kwarg; Bedrock Converse rejects it."""
    assert llm_factory.supports_prompt_caching("claude-haiku-5.5") is True
    assert llm_factory.supports_prompt_caching(HAIKU) is False

    msg = {"role": "system", "content": [{"type": "text", "text": "hi",
                                          "cache_control": {"type": "ephemeral"}}]}
    assert llm_factory.system_message_for_model(msg, HAIKU)["content"] == "hi"


# ---------------------------------------------------------------- credentials

def test_credentials_come_from_the_instance_role_only():
    session = bedrock.role_only_session()
    resolver = session.get_component("credential_provider")

    assert [p.METHOD for p in resolver.providers] == ["container-role", "iam-role"]
    assert session.get_config_variable("ec2_metadata_v1_disabled") is True  # IMDSv2


def test_static_keys_in_the_env_are_never_used(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    resolver = bedrock.role_only_session().get_component("credential_provider")

    assert "env" not in [p.METHOD for p in resolver.providers]


def test_static_keys_make_bedrock_unavailable(monkeypatch):
    monkeypatch.setenv(bedrock.REGION_ENV, "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    with patch.object(bedrock, "_role_credentials", return_value=object()):
        assert bedrock.available() is False


# ---------------------------------------------------------------- availability

def test_unconfigured_box_is_unavailable_without_probing_aws():
    with patch.object(bedrock, "_role_credentials", side_effect=AssertionError("probed")):
        assert bedrock.available() is False


def test_available_with_region_and_role_credentials(monkeypatch):
    monkeypatch.setenv(bedrock.REGION_ENV, "us-west-2")
    with patch.object(bedrock, "_role_credentials", return_value=object()):
        assert bedrock.available() is True


def test_unavailable_when_the_role_cannot_be_reached(monkeypatch):
    monkeypatch.setenv(bedrock.REGION_ENV, "us-west-2")
    with patch.object(bedrock, "_role_credentials", side_effect=RuntimeError("IMDS timeout")):
        assert bedrock.available() is False


def test_availability_is_cached(monkeypatch):
    monkeypatch.setenv(bedrock.REGION_ENV, "us-west-2")
    with patch.object(bedrock, "_role_credentials", return_value=object()) as probe:
        bedrock.available()
        bedrock.available()
    assert probe.call_count == 1


def test_unavailable_without_boto3(monkeypatch):
    monkeypatch.setenv(bedrock.REGION_ENV, "us-west-2")
    monkeypatch.setitem(sys.modules, "botocore.session", None)
    assert bedrock.available() is False


@pytest.mark.asyncio
async def test_available_models_greys_it_out_on_a_normal_box():
    from app.routers.api import available_models

    with patch("app.routers.api.is_model_enabled", return_value=True):
        result = await available_models(MagicMock())
    entry = next(m for m in result["models"] if m["value"] == HAIKU)
    assert entry["available"] is False
    assert entry["reason"] == "AWS Bedrock is not set up on this box"


@pytest.mark.asyncio
async def test_available_models_lights_it_up_on_an_aws_box(monkeypatch):
    from app.routers.api import available_models

    monkeypatch.setenv(bedrock.REGION_ENV, "us-west-2")
    with patch.object(bedrock, "available", return_value=True), \
         patch("app.routers.api.is_model_enabled", return_value=True):
        result = await available_models(MagicMock())
    entry = next(m for m in result["models"] if m["value"] == HAIKU)
    assert entry["available"] is True


# ---------------------------------------------------------------- ZDR

def test_haiku_bedrock_is_zdr_compliant_in_the_build():
    assert ZDR_COMPLIANT[HAIKU] is True
    assert zdr.is_zdr_compliant(HAIKU) is True


def test_zdr_box_still_needs_the_mothership_to_allow_it():
    not_listed = zdr.ZDRState(True, ("deepseek-v4.1-flash-fireworks",), "ferpa")
    listed = zdr.ZDRState(True, (HAIKU,), "ferpa")

    assert HAIKU not in zdr.permitted_models(not_listed)
    assert zdr.permitted_models(listed) == [HAIKU]
