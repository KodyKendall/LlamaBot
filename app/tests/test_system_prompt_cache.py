"""
Tests for runtime, mothership-delivered agent system prompts.

Covers the LlamaBot half of the "dynamic system prompts from the mothership"
feature (task 2026-06-27):
- `resolve_base_prompt` adopts a cached override only when present AND plausibly
  complete (corruption guard), else falls back to the baked-in static prompt.
- `system_prompt_cache.get_cached` is fail-open: any DB error → None.
- `MothershipClient.check_updates` sends the cached `prompt_versions` and
  persists any `system_prompts` blob in the response.
- Backwards-compat: a response with no `system_prompts` key never crashes.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.services.mothership_client import MothershipClient
from app.services import system_prompt_cache
from app.agents.leonardo.project_context import resolve_base_prompt, MIN_PROMPT_LEN


FAKE_CONFIG = {
    "instance_name": "test-instance",
    "mothership_url": "https://mothership.example.com",
    "mothership_api_token": "tok-test",
    "lease_duration_seconds": 300,
}

LONG_BODY = "X" * (MIN_PROMPT_LEN + 50)
STATIC = "STATIC BASE PROMPT " * 50  # a real base prompt is always long


def _make_client() -> MothershipClient:
    client = MothershipClient.__new__(MothershipClient)
    client.config = FAKE_CONFIG
    return client


def _mock_response(status=200, body=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.raise_for_status = MagicMock()
    return resp


def _patch_http(captured, response_body):
    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        captured["url"] = url
        return _mock_response(body=response_body)

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=fake_post)
    return mock_http


# ----------------------------------------------------------------------------
# resolve_base_prompt
# ----------------------------------------------------------------------------

def test_resolve_returns_cached_when_present():
    with patch.object(system_prompt_cache, "get_cached", return_value=LONG_BODY):
        assert resolve_base_prompt(STATIC, "rails_agent") == LONG_BODY


def test_resolve_returns_static_when_absent():
    with patch.object(system_prompt_cache, "get_cached", return_value=None):
        assert resolve_base_prompt(STATIC, "rails_agent") == STATIC


def test_resolve_returns_static_when_cached_too_short():
    # Corruption guard: a short/garbled body must NOT replace the real prompt.
    with patch.object(system_prompt_cache, "get_cached", return_value="too short"):
        assert resolve_base_prompt(STATIC, "rails_agent") == STATIC


def test_resolve_returns_static_when_no_agent_mode():
    # agent_mode=None preserves today's behavior exactly — must not hit the cache.
    with patch.object(system_prompt_cache, "get_cached") as gc:
        assert resolve_base_prompt(STATIC, None) == STATIC
        gc.assert_not_called()


# ----------------------------------------------------------------------------
# system_prompt_cache fail-open
# ----------------------------------------------------------------------------

def test_get_cached_returns_none_on_db_error():
    # A blown-up Session must degrade to None, never raise.
    with patch("app.db.engine", MagicMock()), \
         patch("app.services.system_prompt_cache.Session", side_effect=RuntimeError("db down")):
        assert system_prompt_cache.get_cached("rails_agent") is None


def test_cached_versions_returns_empty_on_db_error():
    with patch("app.db.engine", MagicMock()), \
         patch("app.services.system_prompt_cache.Session", side_effect=RuntimeError("db down")):
        assert system_prompt_cache.cached_versions() == {}


def test_upsert_swallows_db_error():
    # upsert must never raise — a DB hiccup can't break the fail-open update check.
    with patch("app.db.engine", MagicMock()), \
         patch("app.services.system_prompt_cache.Session", side_effect=RuntimeError("db down")):
        system_prompt_cache.upsert("rails_agent", "v1", LONG_BODY)  # no exception


# ----------------------------------------------------------------------------
# check_updates wiring
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_updates_sends_prompt_versions():
    client = _make_client()
    captured = {}
    mock_http = _patch_http(captured, {"updates_available": False})
    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http), \
         patch.object(system_prompt_cache, "cached_versions", return_value={"rails_agent": "88d8a037f815"}), \
         patch.object(system_prompt_cache, "upsert"):
        await client.check_updates("0.5.2a", "0.5.2a")
    assert captured["payload"]["prompt_versions"] == {"rails_agent": "88d8a037f815"}


@pytest.mark.asyncio
async def test_check_updates_upserts_system_prompts():
    client = _make_client()
    captured = {}
    response_body = {
        "updates_available": True,
        "system_prompts": {
            "rails_beginner_agent": {"version": "c46eb10b7bc8", "body": LONG_BODY},
        },
    }
    mock_http = _patch_http(captured, response_body)
    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http), \
         patch.object(system_prompt_cache, "cached_versions", return_value={}), \
         patch.object(system_prompt_cache, "upsert") as mock_upsert:
        result = await client.check_updates("0.5.2a", "0.5.2a")
    mock_upsert.assert_called_once_with("rails_beginner_agent", "c46eb10b7bc8", LONG_BODY)
    assert result["updates_available"] is True


@pytest.mark.asyncio
async def test_check_updates_no_system_prompts_key_no_crash():
    # Old-mothership response (no system_prompts) → no crash, no upsert.
    client = _make_client()
    captured = {}
    mock_http = _patch_http(captured, {"updates_available": False, "latest_versions": {}})
    with patch("app.services.mothership_client.httpx.AsyncClient", return_value=mock_http), \
         patch.object(system_prompt_cache, "cached_versions", return_value={}), \
         patch.object(system_prompt_cache, "upsert") as mock_upsert:
        result = await client.check_updates("0.5.2a", "0.5.2a")
    mock_upsert.assert_not_called()
    assert result["updates_available"] is False


@pytest.mark.asyncio
async def test_check_updates_disabled_client_is_noop():
    # No instance.json → disabled → returns None, never touches the cache.
    client = MothershipClient.__new__(MothershipClient)
    client.config = None
    with patch.object(system_prompt_cache, "upsert") as mock_upsert:
        result = await client.check_updates("0.5.2a", "0.5.2a")
    assert result is None
    mock_upsert.assert_not_called()
