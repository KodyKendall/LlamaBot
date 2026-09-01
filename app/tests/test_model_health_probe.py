"""The box checks its own model with a REAL call (0.7.6).

Meta retired ``muse-spark-1.2-contributor`` on 2026-08-31 at 4:46 PM MDT. Nothing
detected it, and the reason is specific and worth keeping: **``GET /v1/models``
still listed the retired id.** Every health check that reads the model list
reported green throughout the outage. Verified against the provider with two keys —
only the ``-contributor`` tier 404s, and only on a completion.

So the probe has to be a real completion. It is cheap, it runs rarely, and when it
fails it feeds the same dead-model memory the live ladder uses, which means a box
reroutes itself BEFORE a customer types anything.

What it must not do is cry wolf: a probe that marks a model dead on any error would
turn a network blip into a fleet-wide model change.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from app.agents.leonardo import model_health
from app.services.lease_manager import LeaseManager


@pytest.fixture(autouse=True)
def _no_death_records():
    model_health.reset()
    yield
    model_health.reset()


def _make_manager():
    app = MagicMock()
    app.state.timestamp = datetime.now(timezone.utc) - timedelta(seconds=5)
    mothership = MagicMock()
    mothership.enabled = True
    mothership.report_rails_health = AsyncMock(return_value=None)
    mothership.renew_lease = AsyncMock(return_value=None)
    return LeaseManager(app, mothership)


class _Retired(Exception):
    def __init__(self):
        super().__init__("model_not_found")
        self.status_code = 404


@pytest.mark.asyncio
async def test_a_retired_model_is_recorded_by_the_probe():
    """THE DETECTION GAP. This is what nothing caught on 2026-08-31."""
    manager = _make_manager()
    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=_Retired())

    with patch("app.agents.leonardo.model_policy.enabled_default_model", return_value="dead-model"):
        with patch("app.agents.leonardo.llm_factory.get_llm", return_value=llm):
            await manager._probe_default_model()

    assert model_health.is_gone("dead-model") is True


@pytest.mark.asyncio
async def test_a_healthy_model_is_not_marked_dead():
    manager = _make_manager()
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=MagicMock())

    with patch("app.agents.leonardo.model_policy.enabled_default_model", return_value="live-model"):
        with patch("app.agents.leonardo.llm_factory.get_llm", return_value=llm):
            await manager._probe_default_model()

    assert model_health.is_gone("live-model") is False


@pytest.mark.asyncio
async def test_an_ordinary_failure_does_not_condemn_the_model():
    """A timeout or a 500 is the provider having a bad minute, not a retirement.

    Marking a model dead on any error would let one blip move every box.
    """
    manager = _make_manager()
    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=TimeoutError("slow"))

    with patch("app.agents.leonardo.model_policy.enabled_default_model", return_value="slow-model"):
        with patch("app.agents.leonardo.llm_factory.get_llm", return_value=llm):
            await manager._probe_default_model()

    assert model_health.is_gone("slow-model") is False


@pytest.mark.asyncio
async def test_the_probe_is_rate_limited():
    """A real completion on every 5-minute tick is 148 boxes of pointless spend.

    A retirement is permanent, so hourly finds it fast enough.
    """
    manager = _make_manager()
    assert manager.MODEL_PROBE_INTERVAL >= 3600

    calls = []

    async def fake_probe():
        calls.append(1)

    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(200, 10))):
        with patch.object(manager, "_probe_default_model", fake_probe):
            await manager._tick()
            await manager._tick()
            await manager._tick()

    assert len(calls) == 1, "the model probe must not run on every tick"


@pytest.mark.asyncio
async def test_a_failing_probe_never_breaks_the_tick():
    """Same rule as every other piece of telemetry in this loop."""
    manager = _make_manager()

    with patch.object(manager, "_probe_rails", AsyncMock(return_value=(200, 10))):
        with patch.object(manager, "_probe_default_model", AsyncMock(side_effect=RuntimeError("boom"))):
            await manager._tick()

    manager.mothership.report_rails_health.assert_awaited()


# ---------------------------------------------------------------------------
# A retired model is never "effective", even when explicitly allow-listed
# ---------------------------------------------------------------------------
#
# Caught on the live dev box, not by a unit test: the box's instance.json names
# muse-spark-1.2-contributor in enabled_models, so it stayed enabled and
# effective_model handed it straight back — even with a different default configured
# and the model retired. The earlier tests all patch _read_instance_config away, so
# none of them saw it.
#
# The 365-day llmModel cookie means effective_model is the ONLY thing that reaches an
# already-pinned user. An allow-list is the operator saying "this model may be used";
# it is not a claim that the model still exists.

def test_a_retired_model_is_not_effective_even_when_allowlisted(monkeypatch):
    from app.agents.leonardo import model_policy

    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-key")
    monkeypatch.setattr(
        model_policy, "_read_instance_config",
        lambda: {"enabled_models": ["muse-spark-1.2-contributor", "deepseek-v4-flash"]},
    )

    muse = "muse-spark-1.2-contributor"
    # While it is alive the allow-list governs and nothing changes.
    assert model_policy.effective_model(muse) == muse

    model_health.mark_model_gone(muse)
    assert model_policy.effective_model(muse) != muse, (
        "a cookie-pinned user must be moved off a model that reports itself retired"
    )


def test_a_live_allowlisted_model_is_untouched(monkeypatch):
    """The rule is scoped to DEAD models — it must not evict a healthy one."""
    from app.agents.leonardo import model_policy

    monkeypatch.setenv("META_API_KEY", "meta-test-key")
    monkeypatch.setattr(
        model_policy, "_read_instance_config",
        lambda: {"enabled_models": ["muse-spark-1.2-contributor"]},
    )
    assert model_policy.effective_model("muse-spark-1.2-contributor") == "muse-spark-1.2-contributor"
