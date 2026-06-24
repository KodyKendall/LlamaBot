"""Tests for the `enable_browser_inspect` site setting gate.

The headless-browser `browser_inspect` tool is opt-in: it must be ABSENT from an
agent's toolset unless the `enable_browser_inspect` site setting is "true". This
guards against the Playwright/Chromium path being exposed by default.
"""
from unittest.mock import MagicMock, patch

from app.agents.leonardo.rails_agent.tools import browser_inspect_enabled


def _tool_names(tools):
    return {getattr(t, "name", None) for t in tools}


class TestBrowserInspectEnabledHelper:
    def test_disabled_when_auth_db_unavailable(self):
        # engine is None (no AUTH_DB_URI) -> fail closed, tool stays disabled.
        with patch("app.db.engine", None):
            assert browser_inspect_enabled() is False

    def test_disabled_by_default_setting(self):
        with patch("app.db.engine", object()), \
             patch("sqlmodel.Session"), \
             patch("app.routers.api.get_site_setting", return_value="false") as gss:
            assert browser_inspect_enabled() is False
            # Reads the right key with a safe default.
            assert gss.call_args.args[1] == "enable_browser_inspect"
            assert gss.call_args.args[2] == "false"

    def test_enabled_when_setting_true(self):
        with patch("app.db.engine", object()), \
             patch("sqlmodel.Session"), \
             patch("app.routers.api.get_site_setting", return_value="true"):
            assert browser_inspect_enabled() is True


class TestRailsAgentGate:
    def _tools(self, enabled):
        from app.agents.leonardo.rails_agent import nodes
        with patch.object(nodes, "browser_inspect_enabled", return_value=enabled):
            return _tool_names(nodes.agent_tools())

    def test_browser_inspect_absent_by_default(self):
        assert "browser_inspect" not in self._tools(enabled=False)

    def test_browser_inspect_present_when_enabled(self):
        assert "browser_inspect" in self._tools(enabled=True)


class TestBeginnerAgentGate:
    def _build_capturing_toolnode(self, enabled):
        captured = {}

        def fake_toolnode(tools):
            captured["tools"] = tools
            return MagicMock()

        from app.agents.leonardo.rails_beginner_agent import nodes
        with patch.object(nodes, "ToolNode", side_effect=fake_toolnode), \
             patch.object(nodes, "browser_inspect_enabled", return_value=enabled):
            nodes.build_workflow()
        return _tool_names(captured["tools"])

    def test_browser_inspect_absent_by_default(self):
        assert "browser_inspect" not in self._build_capturing_toolnode(enabled=False)

    def test_browser_inspect_present_when_enabled(self):
        assert "browser_inspect" in self._build_capturing_toolnode(enabled=True)
