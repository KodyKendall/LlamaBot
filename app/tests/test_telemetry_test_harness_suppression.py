"""Harness output must not land in production telemetry.

94 of the 140 `instance_errors` occurrences tagged `llamabot_version = 0.7.0`
came from `llamapress-dev` and are synthetic: summarization fixtures whose
numbers repeat exactly across days (messages named `h1`, `big`, `a0`, `a1`),
and the WebSocket integration thread. The dashboards hide internal boxes, so
triage was not distorted — but the rows still land in the production table, and
a real failure on the dev box is now a needle in that haystack.

The kill switch is an env var so any harness (pytest, CI, a scripted E2E run)
can opt out with one line, and `app/tests/conftest.py` sets it for the whole
suite.

It gates `reporting_enabled`, NOT `enabled`. The first version of this feature
gated `enabled`, which also carries login verification, the paywall check,
update checks and lease renewal — so setting the var did not silence a harness,
it signed it out. The scope tests below are the guard against that coming back.
"""

import os

import pytest

from app.services.mothership_client import MothershipClient, TELEMETRY_DISABLED_ENV


CONFIG = {
    "instance_name": "llamapress-dev",
    "mothership_url": "https://llamapress.ai",
    "mothership_api_token": "token",
}


def _client(monkeypatch, config=CONFIG):
    monkeypatch.setattr(MothershipClient, "_load_config", lambda self: config)
    return MothershipClient()


def test_the_test_suite_disables_telemetry_for_itself():
    """conftest sets this before anything imports the client."""
    assert os.environ.get(TELEMETRY_DISABLED_ENV) == "1"


def test_a_fully_configured_client_stops_reporting(monkeypatch):
    client = _client(monkeypatch)
    assert client.reporting_enabled is False


def test_the_switch_does_not_sign_the_box_out(monkeypatch):
    """`enabled` must stay a pure config check.

    It gates verify_login_grant, check_paywall, check_updates and renew_lease —
    the last of which carries the instance-lock backstop. A harness that wanted
    silence must not lose sign-in, and an operator who sets the var on a real box
    must not either.
    """
    client = _client(monkeypatch)
    assert client.enabled is True


def test_without_the_env_var_it_reports_as_normal(monkeypatch):
    monkeypatch.delenv(TELEMETRY_DISABLED_ENV, raising=False)
    client = _client(monkeypatch)
    assert client.reporting_enabled is True


def test_an_unconfigured_box_still_reports_nothing(monkeypatch):
    """The narrow gate is AND, not a replacement for the config check."""
    monkeypatch.delenv(TELEMETRY_DISABLED_ENV, raising=False)
    assert _client(monkeypatch, config=None).reporting_enabled is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_truthy_spellings_all_disable(monkeypatch, value):
    monkeypatch.setenv(TELEMETRY_DISABLED_ENV, value)
    assert _client(monkeypatch).reporting_enabled is False


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_falsey_spellings_leave_it_on(monkeypatch, value):
    monkeypatch.setenv(TELEMETRY_DISABLED_ENV, value)
    assert _client(monkeypatch).reporting_enabled is True


@pytest.mark.asyncio
async def test_no_report_is_posted_while_disabled(monkeypatch):
    """Every reporter goes through `reporting_enabled`, so nothing reaches the network."""
    import httpx

    def explode(*args, **kwargs):
        raise AssertionError("a test run posted to the mothership")

    monkeypatch.setattr(httpx, "AsyncClient", explode)
    client = _client(monkeypatch)

    assert await client.report_error(
        thread_id="ws_integration_thread",
        error_class="AttributeError",
        error_message="'NoneType' object has no attribute 'astream'",
        traceback_str="…",
    ) is None

    # Friction reports ride the same pipeline (send_friction_report ->
    # report_error), so the fixtures named h1/big/a0 are covered too.
    from app.agents.leonardo.friction import send_friction_report

    sent = await send_friction_report(
        {
            "error_class": "AgentFriction",
            "what_happened": "never completes a turn",
            "details": "5000 tokens remaining",
        },
        mothership=client,
    )
    assert sent is False
