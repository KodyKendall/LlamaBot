"""
Tests for `report_friction` — the agent-facing papercut channel.

Leos hit friction constantly (a tool erroring in a way its description never
warned about, a root-owned file they can't edit, output that contradicts the
docs) and until now it died in the transcript. This tool lets the agent file it
against the SAME mothership error pipeline the backend/frontend already use
(`MothershipClient.report_error`, `docs/dev/error_telemetry.md`), tagged
`source="agent_friction"` so self-reported papercuts can be filtered apart from
real exceptions.

Structural assertions only — we never assert LLM behaviour, and we never call
build_workflow() in a unit test (it clears the asyncio event loop; see memory
`ci_build_workflow_event_loop`). We assert the payload contract, the
dedupe/cap guard, tool registration per mode, and prompt wiring.
"""
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.leonardo import friction


def _runtime(thread_id="thread-1", agent_mode="rails_agent", model="deepseek-v4-flash"):
    """A minimal stand-in for langchain's injected ToolRuntime."""
    return SimpleNamespace(
        state={"agent_mode": agent_mode, "llm_model": model},
        config={"configurable": {"thread_id": thread_id}},
        tool_call_id="call-1",
    )


def _invoke(runtime=None, **kwargs):
    """Call the tool the way ToolNode does, with runtime injected."""
    args = {
        "what_happened": "edit_file refused to write app/models/user.rb: Permission denied.",
        "category": "permissions",
        "severity": "blocked",
    }
    args.update(kwargs)
    return friction.report_friction.func(runtime=runtime or _runtime(), **args)


@pytest.fixture(autouse=True)
def _clean_tracking():
    friction.reset_friction_tracking()
    yield
    friction.reset_friction_tracking()


class TestFrictionReportPayload:
    """The report the mothership receives has to be triageable on its own."""

    def test_build_report_carries_triage_fields(self):
        report = friction.build_friction_report(
            what_happened="edit_file said Permission denied on a root-owned file.",
            category="permissions",
            severity="blocked",
            tool_name="edit_file",
            evidence="PermissionError: [Errno 13] Permission denied: '/app/rails/app/models/user.rb'",
            suggested_fix="Have edit_file suggest fix_permissions on EACCES.",
            thread_id="thread-abc",
            agent_mode="rails_agent",
            model="deepseek-v4-flash",
        )

        assert report["category"] == "permissions"
        assert report["severity"] == "blocked"
        assert report["tool_name"] == "edit_file"
        assert report["thread_id"] == "thread-abc"
        assert report["agent_mode"] == "rails_agent"
        assert report["model"] == "deepseek-v4-flash"
        # error_class is what the Error Queue groups on — it must name the
        # channel AND the category, so friction never looks like a real crash.
        assert report["error_class"] == "AgentFriction.permissions"
        assert report["fingerprint"]
        assert report["occurred_at"]
        # The verbatim evidence and the suggested fix are the two highest-value
        # fields for whoever triages this; both ride in the details blob.
        assert "Errno 13" in report["details"]
        assert "fix_permissions on EACCES" in report["details"]
        assert "blocked" in report["details"]

    def test_unknown_category_and_severity_are_coerced_not_rejected(self):
        """A bad enum must never derail the agent's turn — coerce and move on."""
        report = friction.build_friction_report(
            what_happened="something weird",
            category="banana",
            severity="catastrophic",
            thread_id="t",
        )
        assert report["category"] == "other"
        assert report["severity"] == "annoyance"

    def test_long_fields_are_truncated(self):
        report = friction.build_friction_report(
            what_happened="x" * 50_000,
            category="tool_error",
            severity="annoyance",
            evidence="y" * 50_000,
            thread_id="t",
        )
        assert len(report["what_happened"]) <= friction.MAX_WHAT_HAPPENED
        assert len(report["details"]) <= friction.MAX_DETAILS

    def test_fingerprint_is_stable_and_discriminating(self):
        a = friction.friction_fingerprint("tool_error", "edit_file", "boom happened")
        b = friction.friction_fingerprint("tool_error", "edit_file", "boom happened")
        c = friction.friction_fingerprint("tool_error", "write_file", "boom happened")
        assert a == b
        assert a != c


class TestSendToMothership:
    """Same path as backend/frontend errors, tagged so it can be filtered apart."""

    @pytest.mark.asyncio
    async def test_send_posts_via_report_error_with_agent_friction_source(self):
        mothership = SimpleNamespace(enabled=True, report_error=AsyncMock(return_value=None))
        report = friction.build_friction_report(
            what_happened="tail_rails_logs returned binary garbage.",
            category="confusing_output",
            severity="workaround",
            tool_name="tail_rails_logs",
            thread_id="thread-xyz",
            agent_mode="rails_beginner_agent",
            model="claude-haiku-4-5",
        )

        ok = await friction.send_friction_report(report, mothership=mothership)

        assert ok is True
        kwargs = mothership.report_error.await_args.kwargs
        assert kwargs["source"] == friction.FRICTION_SOURCE == "agent_friction"
        assert kwargs["error_class"] == "AgentFriction.confusing_output"
        assert kwargs["thread_id"] == "thread-xyz"
        assert kwargs["agent_mode"] == "rails_beginner_agent"
        assert kwargs["model"] == "claude-haiku-4-5"
        assert kwargs["fingerprint"] == report["fingerprint"]
        # severity=workaround → the agent got past it, so it is "recovered".
        assert kwargs["recovered"] is True

    @pytest.mark.asyncio
    async def test_blocked_severity_reports_as_not_recovered(self):
        mothership = SimpleNamespace(enabled=True, report_error=AsyncMock(return_value=None))
        report = friction.build_friction_report(
            what_happened="could not proceed", category="permissions",
            severity="blocked", thread_id="t",
        )
        await friction.send_friction_report(report, mothership=mothership)
        assert mothership.report_error.await_args.kwargs["recovered"] is False

    @pytest.mark.asyncio
    async def test_send_is_a_noop_when_mothership_disabled(self):
        mothership = SimpleNamespace(enabled=False, report_error=AsyncMock())
        report = friction.build_friction_report(
            what_happened="x", category="other", severity="annoyance", thread_id="t",
        )
        assert await friction.send_friction_report(report, mothership=mothership) is False
        mothership.report_error.assert_not_awaited()

    def test_dispatch_runs_the_send_without_blocking_the_caller(self):
        """The agent must never sit on a network round trip to file a complaint."""
        done = threading.Event()
        captured = {}

        async def fake_send(report, mothership=None):
            captured["report"] = report
            done.set()
            return True

        with patch.object(friction, "send_friction_report", fake_send):
            friction.dispatch_friction_report({"marker": "sentinel"})
            assert done.wait(timeout=5), "dispatch never ran the send"

        assert captured["report"] == {"marker": "sentinel"}

    @pytest.mark.asyncio
    async def test_send_never_raises_when_the_post_fails(self):
        """A telemetry hiccup must never worsen the turn the agent is in."""
        mothership = SimpleNamespace(
            enabled=True, report_error=AsyncMock(side_effect=RuntimeError("mothership down")),
        )
        report = friction.build_friction_report(
            what_happened="x", category="other", severity="annoyance", thread_id="t",
        )
        assert await friction.send_friction_report(report, mothership=mothership) is False


class TestToolBehaviour:
    """The tool must be cheap, non-blocking, and impossible to trip over."""

    def test_tool_dispatches_a_report_and_acks_without_blocking(self):
        with patch.object(friction, "dispatch_friction_report") as dispatch:
            result = _invoke(tool_name="edit_file")

        assert dispatch.call_count == 1
        report = dispatch.call_args.args[0]
        assert report["category"] == "permissions"
        assert report["tool_name"] == "edit_file"
        # The ack must tell the model this changed nothing so it keeps working
        # instead of stopping to explain itself to the user.
        assert "recorded" in result.lower()
        assert "continue" in result.lower()

    def test_repeat_report_in_same_thread_is_dropped(self):
        """A retry loop must not post the same papercut twenty times."""
        with patch.object(friction, "dispatch_friction_report") as dispatch:
            _invoke(tool_name="edit_file")
            second = _invoke(tool_name="edit_file")

        assert dispatch.call_count == 1
        # Still not an error — an error string invites the model to retry.
        assert "already" in second.lower()

    def test_reports_are_capped_per_thread(self):
        with patch.object(friction, "dispatch_friction_report") as dispatch:
            for i in range(friction.MAX_REPORTS_PER_THREAD + 4):
                _invoke(what_happened=f"distinct problem number {i}", tool_name=f"tool_{i}")

        assert dispatch.call_count == friction.MAX_REPORTS_PER_THREAD

    def test_cap_is_per_thread_not_global(self):
        with patch.object(friction, "dispatch_friction_report") as dispatch:
            _invoke(runtime=_runtime(thread_id="thread-a"), tool_name="edit_file")
            _invoke(runtime=_runtime(thread_id="thread-b"), tool_name="edit_file")

        assert dispatch.call_count == 2

    def test_tool_never_raises_even_with_a_broken_runtime(self):
        result = friction.report_friction.func(
            what_happened="x", category="other", severity="annoyance", runtime=None,
        )
        assert isinstance(result, str)

    def test_tool_description_names_the_triggers_and_the_non_triggers(self):
        desc = friction.report_friction.description
        for trigger in ["permission", "did not expect", "work around", "confusing"]:
            assert trigger in desc.lower(), f"missing trigger: {trigger}"
        # The single most common misfire: reporting the user's app bugs.
        assert "user" in desc.lower()
        for field in ["what_happened", "category", "severity", "evidence", "suggested_fix"]:
            assert field in desc


TARGET_MODES = [
    ("engineer",      "app.agents.leonardo.rails_agent.nodes"),
    ("engineer plan", "app.agents.leonardo.rails_engineer_plan_mode_agent.nodes"),
    ("beginner",      "app.agents.leonardo.rails_beginner_agent.nodes"),
    ("beginner plan", "app.agents.leonardo.rails_plan_mode_agent.nodes"),
    ("ticket",        "app.agents.leonardo.rails_ticket_mode_agent.nodes"),
    ("ticket plan",   "app.agents.leonardo.rails_ticket_plan_mode_agent.nodes"),
]


class TestRegisteredInEveryMode:
    """A papercut channel only works if EVERY mode can reach it."""

    @pytest.mark.parametrize("label,module_name", TARGET_MODES, ids=[m[0] for m in TARGET_MODES])
    def test_report_friction_is_in_default_tools(self, label, module_name):
        module = __import__(module_name, fromlist=["default_tools"])
        names = [t.name for t in module.default_tools]
        assert "report_friction" in names, f"{label} mode cannot report friction"

    def test_beginner_binds_report_friction_to_the_llm(self):
        """Beginner's raw StateGraph binds its own list — the model must SEE the tool.

        Its per-turn bind list is a separate list from `default_tools` (which only
        feeds ToolNode), so a tool added to one and not the other is executable
        but invisible.
        """
        from app.agents.leonardo.rails_beginner_agent.nodes import beginner_turn_tools

        assert "report_friction" in [t.name for t in beginner_turn_tools()]

    @pytest.mark.parametrize("module_name", [
        "app.agents.leonardo.rails_agent.sub_agents",
        "app.agents.leonardo.rails_ticket_mode_agent.sub_agents",
    ])
    def test_delegated_sub_agents_can_report_friction(self, monkeypatch, module_name):
        """Sub-agents run in isolated context — friction they hit is friction the
        main agent never witnesses, so it has to be reportable from in there."""
        # Building the sub-agent constructs a real chat client, and ChatDeepSeek
        # refuses to instantiate without a key even though nothing here calls out
        # to the network. CI has no DEEPSEEK_API_KEY, so supply a placeholder.
        monkeypatch.setenv("DEEPSEEK_API_KEY", os.getenv("DEEPSEEK_API_KEY") or "test-key")
        module = __import__(module_name, fromlist=["create_sub_agent"])
        agent = module.create_sub_agent(llm_model="deepseek-v4-flash")
        node = agent.nodes.get("tools")
        bound = getattr(node, "bound", node)
        assert "report_friction" in getattr(bound, "tools_by_name", {})


class TestPromptWiring:
    """The tool description alone won't get it used — the mode prompt must ask."""

    def test_prompt_section_states_the_trigger_and_the_carry_on_rule(self):
        section = friction.FRICTION_PROMPT_SECTION
        assert "report_friction" in section
        lowered = section.lower()
        # Without these two rules the model either stops after reporting or
        # narrates the report at the user.
        assert "user" in lowered
        assert "continue" in lowered or "carry on" in lowered

    def test_with_friction_section_appends_once(self):
        base = "SOME PROMPT"
        once = friction.with_friction_section(base)
        assert friction.FRICTION_PROMPT_SECTION in once
        assert friction.with_friction_section(once) == once

    @pytest.mark.parametrize("label,module_name", TARGET_MODES, ids=[m[0] for m in TARGET_MODES])
    def test_mode_system_prompt_includes_the_friction_section(self, label, module_name):
        """Built prompt, not the raw constant — the section must survive a
        mothership prompt override, which replaces the base prompt wholesale."""
        module = __import__(module_name, fromlist=["get_cached_system_prompt"])
        builder = getattr(module, "get_cached_system_prompt", None) or module.get_sys_msg

        # Force the override path: a mothership-delivered prompt replaces the
        # static constant entirely, and the friction section must still be there.
        override = "OVERRIDDEN PROMPT BODY. " * 200
        with patch("app.services.system_prompt_cache.get_cached", return_value=override):
            message = builder()

        content = message.content if hasattr(message, "content") else message["content"]
        text = content if isinstance(content, str) else "".join(
            block.get("text", "") for block in content
        )
        assert "OVERRIDDEN PROMPT BODY" in text, f"{label}: override path not exercised"
        assert "report_friction" in text, f"{label} mode never tells the agent about the tool"
