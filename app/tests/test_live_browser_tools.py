"""Tests for the live-browser tools (navigate/logs/execute-js in the user's tab).

Covers:
- the `enable_live_browser_tools` site-setting gate (fail-closed, default off),
- inclusion/exclusion in rails_agent's toolset,
- the browser_command interrupt payload + ToolMessage wiring,
- the `_check_and_send_interrupts` browser_command -> WS frame mapping,
- the regression guard that browser_command is NOT a question-type interrupt
  (user chat text must never resume it as a fake browser result).

Deliberately does NOT call build_workflow (known CI event-loop issue).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.leonardo.rails_agent.tools import (
    live_browser_tools_enabled,
    navigate_browser,
    get_browser_js_logs,
    execute_browser_js,
    _browser_command,
    BROWSER_COMMAND_RESULT_MAX_CHARS,
)

LIVE_BROWSER_TOOL_NAMES = {"navigate_browser", "get_browser_js_logs", "execute_browser_js"}


def _tool_names(tools):
    return {getattr(t, "name", None) for t in tools}


class TestLiveBrowserToolsEnabledHelper:
    def test_disabled_when_auth_db_unavailable(self):
        # engine is None (no AUTH_DB_URI) -> fail closed, tools stay disabled.
        with patch("app.db.engine", None):
            assert live_browser_tools_enabled() is False

    def test_disabled_by_default_setting(self):
        with patch("app.db.engine", object()), \
             patch("sqlmodel.Session"), \
             patch("app.routers.api.get_site_setting", return_value="false") as gss:
            assert live_browser_tools_enabled() is False
            # Reads the right key with a safe default.
            assert gss.call_args.args[1] == "enable_live_browser_tools"
            assert gss.call_args.args[2] == "false"

    def test_enabled_when_setting_true(self):
        with patch("app.db.engine", object()), \
             patch("sqlmodel.Session"), \
             patch("app.routers.api.get_site_setting", return_value="true"):
            assert live_browser_tools_enabled() is True


class TestRailsAgentGate:
    def _tools(self, enabled):
        from app.agents.leonardo.rails_agent import nodes
        with patch.object(nodes, "live_browser_tools_enabled", return_value=enabled), \
             patch.object(nodes, "browser_inspect_enabled", return_value=False):
            return _tool_names(nodes.agent_tools())

    def test_live_browser_tools_absent_by_default(self):
        assert not (LIVE_BROWSER_TOOL_NAMES & self._tools(enabled=False))

    def test_live_browser_tools_present_when_enabled(self):
        assert LIVE_BROWSER_TOOL_NAMES <= self._tools(enabled=True)


class TestBrowserCommandInterrupt:
    def _invoke(self, tool_fn, expected_payload, **kwargs):
        """Invoke a tool function with a patched interrupt(); return (payload, Command)."""
        captured = {}

        def fake_interrupt(payload):
            captured["payload"] = payload
            return '{"ok": true}'

        runtime = MagicMock(tool_call_id="tc1")
        with patch("app.agents.leonardo.rails_agent.tools.interrupt", side_effect=fake_interrupt):
            cmd = tool_fn.func(runtime=runtime, **kwargs)

        assert captured["payload"] == expected_payload
        return cmd

    def _tool_message(self, cmd):
        messages = cmd.update["messages"]
        assert len(messages) == 1
        return messages[0]

    def test_navigate_browser_payload_and_tool_message(self):
        cmd = self._invoke(
            navigate_browser,
            {"type": "browser_command", "command": "navigate", "args": {"path": "/users"}},
            path="/users",
        )
        msg = self._tool_message(cmd)
        assert msg.tool_call_id == "tc1"
        assert msg.content == '{"ok": true}'

    def test_get_browser_js_logs_payload(self):
        cmd = self._invoke(
            get_browser_js_logs,
            {"type": "browser_command", "command": "get_js_logs", "args": {}},
        )
        assert self._tool_message(cmd).tool_call_id == "tc1"

    def test_execute_browser_js_payload(self):
        cmd = self._invoke(
            execute_browser_js,
            {"type": "browser_command", "command": "execute_js", "args": {"code": "1+1"}},
            code="1+1",
        )
        assert self._tool_message(cmd).tool_call_id == "tc1"

    def test_result_truncated_at_cap(self):
        huge = "x" * (BROWSER_COMMAND_RESULT_MAX_CHARS + 1000)
        with patch("app.agents.leonardo.rails_agent.tools.interrupt", return_value=huge):
            cmd = _browser_command("execute_js", {"code": "big()"}, "tc2")
        content = cmd.update["messages"][0].content
        assert content.endswith("...[truncated]")
        assert len(content) == BROWSER_COMMAND_RESULT_MAX_CHARS + len("...[truncated]")


class TestCheckAndSendInterruptsMapping:
    def _run_check(self, interrupt_value):
        """Drive _check_and_send_interrupts with a stubbed state snapshot; return sent frames."""
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)  # skip __init__ plumbing
        handler._is_websocket_open = MagicMock(return_value=True)

        intr = MagicMock()
        intr.value = interrupt_value
        task = MagicMock()
        task.interrupts = [intr]
        snapshot = MagicMock()
        snapshot.tasks = [task]

        app = MagicMock()
        app.aget_state = AsyncMock(return_value=snapshot)

        websocket = MagicMock()
        websocket.send_json = AsyncMock()

        message_data = {"thread_id": "t-1", "agent_name": "rails_agent"}
        found = asyncio.run(
            handler._check_and_send_interrupts(app, {"configurable": {}}, message_data, websocket)
        )
        return found, [c.args[0] for c in websocket.send_json.call_args_list]

    def test_browser_command_frame_shape(self):
        found, frames = self._run_check(
            {"type": "browser_command", "command": "execute_js", "args": {"code": "1+1"}}
        )
        assert found is True
        assert frames == [{
            "type": "browser_command",
            "command": "execute_js",
            "args": {"code": "1+1"},
            "thread_id": "t-1",
            "agent_name": "rails_agent",
        }]

    def test_browser_command_missing_args_defaults(self):
        found, frames = self._run_check({"type": "browser_command", "command": "get_js_logs"})
        assert found is True
        assert frames[0]["args"] == {}


def test_browser_command_is_not_a_question_interrupt_type():
    # Regression guard: if browser_command were listed here, a user's chat message
    # typed while a browser command is pending would be fed into the tool as a
    # fabricated browser result (see _pending_question_interrupt).
    from app.websocket.request_handler import RequestHandler
    assert "browser_command" not in RequestHandler._QUESTION_INTERRUPT_TYPES
