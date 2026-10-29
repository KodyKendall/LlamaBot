"""LP-21: the user picks the backup model, and the banner says why it was needed.

Michaela's report (2026-10-27): Muse failed mid-turn, Leo quietly finished on some
other model, and nothing said which one or why. Two defects:

  * the fallback was whatever ``fallback_model()`` reached first in code order —
    the user had no say, and the answer moved whenever the fleet chain moved;
  * the ``model_substituted`` frame carried no reason, so the banner could only
    ever say "stopped responding", whatever actually happened.

Structure only — which model was chosen, which fields the frame carries. Never
banner wording; that lives in the JS test.
"""
import asyncio

import httpx
import openai
import pytest

from app.agents.leonardo import model_policy, resilience
from app.agents.leonardo.rails_agent import middleware as mw

MUSE_13 = "muse-spark-1.3-contributor"
DS = "deepseek-v4-flash"
DS_41 = "deepseek-v4.1-flash"
DS_PRO = "deepseek-v4-pro"
MUSE_12 = "muse-spark-1.2-contributor"


@pytest.fixture(autouse=True)
def _box(monkeypatch):
    """Meta + DeepSeek keys, four models allowed, no pushed policy, no backup set."""
    monkeypatch.setattr(model_policy, "_read_instance_config", lambda: None)
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {})
    for var in ("OPENROUTER_API_KEY", "DISABLED_MODELS", "DEFAULT_LLM_MODEL", "MODEL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ENABLED_MODELS", ",".join([MUSE_13, MUSE_12, DS, DS_41, DS_PRO]))
    monkeypatch.setenv("MODEL_SWITCHING_ALLOWED", "true")
    monkeypatch.setenv("META_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-credential")
    monkeypatch.setattr(model_policy.model_health, "is_gone", lambda name: False)
    token = model_policy.start_turn_backup_model(None)
    yield
    model_policy._turn_backup_model.reset(token)


def _with_roles_chat(monkeypatch, chain):
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {"roles": {"chat": chain}})


# ---------------------------------------------------------------------------
# FR-2: the user's backup is consulted first
# ---------------------------------------------------------------------------

def test_the_users_backup_beats_the_fleet_chain(monkeypatch):
    """Scenario 1: DeepSeek V4.1 chosen, Muse 1.3 fails → V4.1, not the chain."""
    _with_roles_chat(monkeypatch, [DS, DS_PRO])
    model_policy.start_turn_backup_model(DS_41)
    assert model_policy.fallback_model(MUSE_13) == DS_41


def test_automatic_still_walks_the_fleet_chain(monkeypatch):
    _with_roles_chat(monkeypatch, [DS_PRO, DS])
    assert model_policy.fallback_model(MUSE_13) == DS_PRO


@pytest.mark.parametrize("why", ["disabled", "no_key", "gone", "same_as_failed", "unknown"])
def test_an_unusable_backup_is_skipped_not_returned(monkeypatch, why):
    _with_roles_chat(monkeypatch, [DS_PRO])
    backup = DS_41
    if why == "disabled":
        monkeypatch.setenv("DISABLED_MODELS", DS_41)
    elif why == "no_key":
        backup = MUSE_12
        monkeypatch.delenv("META_API_KEY")
    elif why == "gone":
        monkeypatch.setattr(model_policy.model_health, "is_gone", lambda n: n == DS_41)
    elif why == "same_as_failed":
        backup = MUSE_13
    elif why == "unknown":
        backup = "not-a-model-we-know"
    model_policy.start_turn_backup_model(backup)

    assert model_policy.fallback_model(MUSE_13) == DS_PRO


def test_a_backup_that_was_already_tried_is_skipped(monkeypatch):
    _with_roles_chat(monkeypatch, [DS_PRO])
    model_policy.start_turn_backup_model(DS_41)
    assert model_policy.fallback_model(MUSE_13, exclude={DS_41}) == DS_PRO


def test_a_customer_paid_turn_never_moves_to_a_platform_backup(monkeypatch):
    from app.agents.leonardo import customer_paid

    monkeypatch.setattr(customer_paid, "required", lambda: True)
    model_policy.start_turn_backup_model(DS_41)
    assert model_policy.fallback_model("gpt-6-luna-chatgpt") != DS_41


# ---------------------------------------------------------------------------
# FR-5: the mothership can name the Automatic backup
# ---------------------------------------------------------------------------

_REAL_REMOTE_POLICY = model_policy.remote_policy


def _install_real_remote_policy(monkeypatch, payload):
    from app.services import model_policy_store

    monkeypatch.setattr(model_policy_store, "load", lambda: payload)
    monkeypatch.setattr(model_policy, "remote_policy", _REAL_REMOTE_POLICY)


def test_a_pushed_fallback_model_reaches_the_box(monkeypatch):
    _install_real_remote_policy(monkeypatch, {"fallback_model": f" {DS_41} "})
    assert model_policy.remote_policy()["fallback_model"] == DS_41


def test_a_malformed_fallback_model_is_dropped_and_the_rest_still_applies(monkeypatch):
    _install_real_remote_policy(monkeypatch, {"fallback_model": ["nope"], "default_model": DS})
    policy = model_policy.remote_policy()
    assert "fallback_model" not in policy
    assert policy["default_model"] == DS


def test_the_policy_fallback_is_used_when_the_user_chose_automatic(monkeypatch):
    monkeypatch.setattr(model_policy, "remote_policy",
                        lambda: {"fallback_model": DS_41, "roles": {"chat": [DS_PRO]}})
    assert model_policy.fallback_model(MUSE_13) == DS_41


def test_the_users_backup_beats_the_policy_fallback(monkeypatch):
    monkeypatch.setattr(model_policy, "remote_policy", lambda: {"fallback_model": DS_PRO})
    model_policy.start_turn_backup_model(DS_41)
    assert model_policy.fallback_model(MUSE_13) == DS_41


# ---------------------------------------------------------------------------
# FR-3: every fallback says why, in a shape the banner can word
# ---------------------------------------------------------------------------

def _status_error(cls, status, body=None, headers=None):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers or {})
    return cls("provider said no", response=response, body=body)


def test_a_retired_model_is_described_as_gone():
    e = _status_error(openai.NotFoundError, 404, {"error": {"code": "model_not_found"}})
    assert resilience.describe_failure(e)["code"] == "model_gone"


def test_an_expired_credential_is_described_as_auth():
    e = _status_error(openai.AuthenticationError, 401)
    assert resilience.describe_failure(e) == {"code": "auth", "status": 401}


def test_a_usage_limit_carries_its_reset_time():
    e = _status_error(openai.RateLimitError, 429, {
        "error": {"type": "usage_limit_reached", "resets_at": 1793145600},
    })
    failure = resilience.describe_failure(e)
    assert failure["code"] == "usage_limit"
    assert failure["resets_at"].startswith("2026-10-28T")


def test_a_plain_rate_limit_is_not_a_usage_limit():
    e = _status_error(openai.RateLimitError, 429, {"error": {"message": "slow down"}})
    assert resilience.describe_failure(e)["code"] == "rate_limited"


def test_a_rejected_request_is_described_as_bad_request():
    e = _status_error(openai.BadRequestError, 400)
    assert resilience.describe_failure(e)["code"] == "bad_request"


def test_a_stall_is_described_as_a_stall():
    assert resilience.describe_failure(TimeoutError("no chunk"))["code"] == "stall"


# ---------------------------------------------------------------------------
# The ladder: 401 and a usage limit move at once, and the frame says why
# ---------------------------------------------------------------------------

class _Model:
    def __init__(self, name):
        self.name = name

    def bind_tools(self, *a, **k):
        return self


class _Req:
    def __init__(self, model_name):
        self.state = {"llm_model": model_name}
        self.model = None
        self.messages = []
        self.selected = []

    def override(self, **kw):
        if kw.get("model") is not None:
            self.model = kw["model"]
            self.selected.append(self.model.name)
        return self


def _run_ladder(monkeypatch, error):
    monkeypatch.setattr(mw, "get_llm", lambda name: _Model(name))
    monkeypatch.setattr(mw, "_model_retry_delay", lambda attempt: 0)
    monkeypatch.setattr(mw, "fallback_model", lambda *a, **k: DS_41)
    frames = []

    async def _send(frame):
        frames.append(frame)

    from app.lib import turn_notices

    turn_notices.start_turn_notices(_send)
    req = _Req(MUSE_13)

    async def handler(r):
        if r.model.name == MUSE_13:
            raise error
        return "answered"

    try:
        result = asyncio.run(mw.DynamicModelMiddleware().awrap_model_call(req, handler))
    finally:
        turn_notices.clear_turn_notices()
    swaps = [f for f in frames if f.get("type") == "model_substituted"]
    return result, req, swaps


@pytest.mark.parametrize("error,code", [
    (lambda: _status_error(openai.AuthenticationError, 401), "auth"),
    (lambda: _status_error(openai.RateLimitError, 429,
                           {"error": {"type": "usage_limit_reached"}}), "usage_limit"),
])
def test_a_dead_credential_moves_to_the_backup_without_retrying(monkeypatch, error, code):
    result, req, swaps = _run_ladder(monkeypatch, error())
    assert result == "answered"
    assert req.selected == [MUSE_13, DS_41], "rung 1 retried an endpoint that cannot answer"
    assert swaps[-1]["failure"]["code"] == code


def test_a_retired_model_fallback_frame_says_why(monkeypatch):
    monkeypatch.setattr(mw.model_health, "mark_model_gone", lambda name: None)
    error = _status_error(openai.NotFoundError, 404, {"error": {"code": "model_not_found"}})
    _, _, swaps = _run_ladder(monkeypatch, error)
    assert swaps[-1]["failure"] == {"code": "model_gone", "status": 404}


def test_a_rejected_request_still_never_reaches_the_fallback(monkeypatch):
    """Unchanged contract: a 400 fails identically everywhere."""
    with pytest.raises(openai.BadRequestError):
        _run_ladder(monkeypatch, _status_error(openai.BadRequestError, 400))


# ---------------------------------------------------------------------------
# FR-1: the websocket frame's backup_model reaches the policy
# ---------------------------------------------------------------------------

def test_the_turn_installs_the_backup_from_the_message():
    from app.websocket.request_handler import RequestHandler

    handler = RequestHandler.__new__(RequestHandler)
    handler._run_manager = lambda: type("RM", (), {"wait_for_writers": None})()
    handler._background_launcher = lambda frame: None
    handler._drain_queued_messages = lambda tid: []
    handler._install_turn_channels({"thread_id": "t1", "backup_model": DS_41})
    assert model_policy.turn_backup_model() == DS_41

    handler._install_turn_channels({"thread_id": "t1"})
    assert model_policy.turn_backup_model() is None, "Automatic must clear a previous pick"
