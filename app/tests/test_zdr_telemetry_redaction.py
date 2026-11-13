"""LP-28: a ZDR box never sends chat text, tool arguments or error text to the mothership.

The mothership drops these fields on receipt for ZDR boxes (2026-10-31). This is the
second, independent layer: the box never puts them on the wire. Structure-only
assertions — what keys leave the box, never what an LLM said.
"""
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.agents.leonardo import zdr
from app.services import mothership_client as mc
from app.services.mothership_client import MothershipClient, TELEMETRY_DISABLED_ENV

FAKE_CONFIG = {
    "instance_name": "test-instance",
    "mothership_url": "https://mothership.example.com",
    "mothership_api_token": "tok-test",
}

STUDENT = "Student Jane Roe SSN 123-45-6789"

PY_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/app/app/agents/leonardo/grades.py", line 42, in load_grades\n'
    f"    raise ValueError('{STUDENT}')\n"
    '  File "/app/app/agents/leonardo/rails_agent/tools.py", line 7, in run\n'
    "    row = lookup(name='Jane Roe')\n"
    f"ValueError: {STUDENT}\n"
)


@pytest.fixture(autouse=True)
def _reporting_on(monkeypatch):
    monkeypatch.delenv(TELEMETRY_DISABLED_ENV, raising=False)


@pytest.fixture
def zdr_on(monkeypatch):
    monkeypatch.setattr(zdr, "enforced", lambda: True)


@pytest.fixture
def zdr_off(monkeypatch):
    monkeypatch.setattr(zdr, "enforced", lambda: False)


def _client():
    client = MothershipClient.__new__(MothershipClient)
    client.config = FAKE_CONFIG
    return client


def _capture():
    captured = {}

    async def fake_post(url, *, json, headers):
        captured["payload"] = json
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        return resp

    http = MagicMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    http.post = AsyncMock(side_effect=fake_post)
    return captured, patch("app.services.mothership_client.httpx.AsyncClient", return_value=http)


TOOL_CALLS = [
    {"name": "write_file", "args": {"path": "students.csv", "content": STUDENT}, "id": "tc-1"},
    {"name": "bash_command", "args": {"command": f"echo '{STUDENT}'"}, "id": "tc-2"},
]


async def _send_message(**overrides):
    captured, patcher = _capture()
    kwargs = dict(
        thread_id="t-1", role="assistant", content=f"Here is {STUDENT}",
        sent_at="2026-11-02T00:00:00+00:00", model="deepseek-v4.1-flash-fireworks",
        token_usage={"input_tokens": 10, "output_tokens": 5},
        tool_calls=TOOL_CALLS, tool_call_id="tc-0", timings={"duration_ms": 12},
    )
    kwargs.update(overrides)
    with patcher:
        await _client().report_message(**kwargs)
    return captured["payload"]


# ---------------------------------------------------------------- report_message

@pytest.mark.asyncio
async def test_zdr_report_message_sends_no_content_and_tool_names_only(zdr_on):
    payload = await _send_message()

    assert payload["content"] is None
    assert payload["tool_calls"] == [{"name": "write_file"}, {"name": "bash_command"}]
    assert STUDENT not in repr(payload)
    # Structural metadata survives.
    assert payload["role"] == "assistant"
    assert payload["model"] == "deepseek-v4.1-flash-fireworks"
    assert payload["token_usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert payload["timings"] == {"duration_ms": 12}
    assert payload["tool_call_id"] == "tc-0"
    assert payload["thread_id"] == "t-1"


@pytest.mark.asyncio
async def test_non_zdr_report_message_is_unchanged(zdr_off):
    payload = await _send_message()

    assert payload["content"] == f"Here is {STUDENT}"
    assert payload["tool_calls"] == TOOL_CALLS


# ---------------------------------------------------------------- report_error

async def _send_error(**overrides):
    captured, patcher = _capture()
    kwargs = dict(
        thread_id="t-1", error_class="ValueError", error_message=STUDENT,
        traceback_str=PY_TRACEBACK, agent_mode="engineer",
        model="deepseek-v4.1-flash-fireworks", llamabot_version="0.7.20",
        occurred_at="2026-11-02T00:00:00+00:00", recovered=False,
    )
    kwargs.update(overrides)
    with patcher:
        await _client().report_error(**kwargs)
    return captured["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["llamabot", "agent_friction", "storage_integrity", "frontend"])
async def test_zdr_report_error_redacts_message_and_traceback(zdr_on, source):
    payload = await _send_error(source=source)

    assert payload["error_message"] == "[redacted: ZDR workspace]"
    assert STUDENT not in repr(payload)
    assert "Jane Roe" not in payload["traceback"]
    assert "raise ValueError" not in payload["traceback"]
    assert payload["traceback"].splitlines() == [
        '  File "/app/app/agents/leonardo/grades.py", line 42, in load_grades',
        '  File "/app/app/agents/leonardo/rails_agent/tools.py", line 7, in run',
    ]
    # Diagnostic metadata survives.
    assert payload["error_class"] == "ValueError"
    assert payload["source"] == source
    assert payload["model"] == "deepseek-v4.1-flash-fireworks"
    assert payload["llamabot_version"] == "0.7.20"
    assert payload["recovered"] is False
    assert payload["occurred_at"] == "2026-11-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_zdr_report_error_fingerprint_is_computed_before_redaction(zdr_on):
    """The server's formula over the REAL message, so ZDR errors still cluster."""
    payload = await _send_error()

    expected = hashlib.md5(f"ValueError|{STUDENT[:160]}|engineer".encode()).hexdigest()
    assert payload["fingerprint"] == expected


@pytest.mark.asyncio
async def test_zdr_report_error_keeps_a_caller_fingerprint(zdr_on):
    payload = await _send_error(fingerprint="update_failed:0.7.20:0.7.20")
    assert payload["fingerprint"] == "update_failed:0.7.20:0.7.20"


@pytest.mark.asyncio
async def test_zdr_report_error_keeps_js_stack_frames_without_query_strings(zdr_on):
    stack = (
        f"TypeError: cannot read {STUDENT}\n"
        "    at renderRow (https://box.llamapress.ai/assets/app.js?token=abc:10:5)\n"
        "    at https://box.llamapress.ai/assets/app.js:20:1\n"
    )
    payload = await _send_error(error_class="TypeError", traceback_str=stack, source="frontend")

    assert payload["traceback"].splitlines() == [
        "    at renderRow (https://box.llamapress.ai/assets/app.js:10:5)",
        "    at https://box.llamapress.ai/assets/app.js:20:1",
    ]


@pytest.mark.asyncio
async def test_non_zdr_report_error_is_unchanged(zdr_off):
    payload = await _send_error()

    assert payload["error_message"] == STUDENT
    assert payload["traceback"] == PY_TRACEBACK
    assert "fingerprint" not in payload  # server computes it, as before


# ---------------------------------------------------------------- turn metrics

METRICS = {
    "total_ms": 900, "ttft_ms": 120, "model_ms": 600, "tool_ms": 200, "overhead_ms": 100,
    "model_calls": 2, "tool_calls": 1, "input_tokens": 3000, "output_tokens": 40,
    "tokens_per_second": 55.5, "compactions": 0,
    "slowest_tool": {"name": "bash_command", "ms": 200},
    "note": STUDENT,
}


@pytest.mark.asyncio
async def test_zdr_turn_metrics_are_numbers_only(zdr_on):
    captured, patcher = _capture()
    with patcher:
        await _client().report_turn_metrics(thread_id="t-1", metrics=dict(METRICS))

    sent = captured["payload"]["metrics"]
    assert set(sent) == {
        "total_ms", "ttft_ms", "model_ms", "tool_ms", "overhead_ms", "model_calls",
        "tool_calls", "input_tokens", "output_tokens", "tokens_per_second",
    }
    assert all(isinstance(v, (int, float)) for v in sent.values())


@pytest.mark.asyncio
async def test_non_zdr_turn_metrics_are_unchanged(zdr_off):
    captured, patcher = _capture()
    with patcher:
        await _client().report_turn_metrics(thread_id="t-1", metrics=dict(METRICS))
    assert captured["payload"]["metrics"] == METRICS


# ---------------------------------------------------------------- feedback

async def _send_feedback():
    captured, patcher = _capture()
    with patcher:
        await _client().submit_feedback(
            thread_id="t-1", rating="down", scope="message", note=f"wrong grade for {STUDENT}",
            content=f"Here is {STUDENT}", message_key="mk-1",
            debug_context={"recent_events": [{"type": "error", "detail": STUDENT}]},
        )
    return captured["payload"]


@pytest.mark.asyncio
async def test_zdr_feedback_sends_rating_only(zdr_on):
    payload = await _send_feedback()

    assert payload["rating"] == "down"
    assert payload["note"] is None
    assert payload["content"] is None
    assert "debug_context" not in payload
    assert STUDENT not in repr(payload)
    assert payload["message_key"] == "mk-1"


@pytest.mark.asyncio
async def test_non_zdr_feedback_is_unchanged(zdr_off):
    payload = await _send_feedback()
    assert payload["note"] == f"wrong grade for {STUDENT}"
    assert payload["content"] == f"Here is {STUDENT}"
    assert "debug_context" in payload


# ---------------------------------------------------------------- fail closed

@pytest.mark.asyncio
async def test_unreadable_zdr_state_redacts(monkeypatch):
    def boom():
        raise RuntimeError("policy store exploded")

    monkeypatch.setattr(zdr, "enforced", boom)
    payload = await _send_message()
    assert payload["content"] is None


# ---------------------------------------------------------------- Rails asks LlamaBot

@pytest.mark.asyncio
async def test_zdr_status_endpoint_reports_the_flag_and_nothing_else(zdr_on):
    """The llama_bot_rails gem polls this to decide whether to redact Rails errors."""
    from app.routers.api import api_zdr_status

    assert await api_zdr_status() == {"zdr": True}


@pytest.mark.asyncio
async def test_zdr_status_endpoint_off(zdr_off):
    from app.routers.api import api_zdr_status

    assert await api_zdr_status() == {"zdr": False}


@pytest.mark.asyncio
async def test_zdr_status_endpoint_needs_no_auth():
    """Rails calls it server-to-server with no user session."""
    from app.routers.api import router

    route = next(r for r in router.routes if getattr(r, "path", "") == "/api/zdr-status")
    assert route.dependant.dependencies == []


# ---------------------------------------------------------------- FR-5: cookbook publishing

from app.agents.leonardo import personal_cookbook_context as pcc  # noqa: E402

ZDR_REFUSAL = "Not available in a zero-data-retention workspace"


@pytest.fixture
def _cold_cookbook():
    pcc.reset_personal_cookbook_cache()
    yield
    pcc.reset_personal_cookbook_cache()


def _prime_recipes():
    pcc.store_personal_recipes(pcc.normalize_personal_recipes({
        "handle": "teacher1",
        "recipes": [{"slug": "gradebook", "title": "Gradebook", "summary": "s",
                     "updated_at": "2026-09-11T17:31:00Z"}],
    }))


def test_zdr_cookbook_block_drops_the_publish_guide(zdr_on, _cold_cookbook):
    _prime_recipes()
    block = pcc.build_personal_cookbook_context()

    assert "gradebook" in block  # reading your own recipes is still fine
    assert pcc.PUBLISH_GUIDE_URL not in block
    assert ZDR_REFUSAL in block


def test_zdr_cookbook_notice_appears_even_with_no_recipes(zdr_on, _cold_cookbook):
    block = pcc.build_personal_cookbook_context()

    assert ZDR_REFUSAL in block
    # Same marker the rails_agent middleware dedups on, so it is not injected twice.
    from app.agents.leonardo.rails_agent.middleware import PersonalCookbookMiddleware
    assert PersonalCookbookMiddleware.MARKER in block


def test_non_zdr_cookbook_block_is_unchanged(zdr_off, _cold_cookbook):
    assert pcc.build_personal_cookbook_context() == ""
    _prime_recipes()
    assert pcc.PUBLISH_GUIDE_URL in pcc.build_personal_cookbook_context()
