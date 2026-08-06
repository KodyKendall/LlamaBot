"""Regression guard: the unvalidated URL-fetching clone agent stays deleted.

Removed 2026-08-06 in response to a coordinated security disclosure. The agent
held two server-side request forgery surfaces, both reachable from an LLM-chosen
URL with no scheme/host/IP validation:

  * ``get_screenshot_and_html_content_using_playwright`` -> ``page.goto(url)``
    (reported), and
  * ``image_clone_agent`` -> ``aiohttp session.get(image_url)`` (found during
    the review of the report).

The feature was experimental and unused, so it was removed rather than guarded.
These tests exist so it does not come back by copy-paste; anything that needs
server-side navigation must go through ``app.agents.utils.url_guard`` instead.
"""
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestModulesAreGone:
    @pytest.mark.parametrize("module", [
        "app.agents.llamapress.clone_agent",
        "app.agents.utils.playwright_screenshot",
    ])
    def test_module_is_not_importable(self, module):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    @pytest.mark.parametrize("path", [
        "app/agents/llamapress/clone_agent.py",
        "app/agents/utils/playwright_screenshot.py",
    ])
    def test_file_is_deleted(self, path):
        assert not (REPO_ROOT / path).exists()


class TestNoDanglingReferences:
    """A stale import of a deleted module breaks graph loading at startup."""

    @pytest.mark.parametrize("path", [
        "app/agents/llamapress/nodes.py",
        "app/agents/leonardo/rails_ai_builder_agent/nodes.py",
    ])
    def test_live_agent_modules_do_not_reference_removed_code(self, path):
        source = (REPO_ROOT / path).read_text()
        for banned in ("clone_agent", "playwright_screenshot", "capture_page_and_img_src"):
            assert banned not in source, f"{path} still references {banned}"

    def test_llamapress_graph_still_imports(self):
        """Removing the clone branch must not break the surviving html_agent path."""
        nodes = importlib.import_module("app.agents.llamapress.nodes")
        assert hasattr(nodes, "build_workflow")


class TestNoUnguardedNavigationRemains:
    def test_no_bare_page_goto_outside_the_guard(self):
        """Every page.goto in the tree must sit behind validate_outbound_url."""
        offenders = []
        for py in (REPO_ROOT / "app").rglob("*.py"):
            if "tests" in py.parts or "__pycache__" in py.parts:
                continue
            source = py.read_text()
            if "page.goto(" in source and "validate_outbound_url" not in source:
                offenders.append(str(py.relative_to(REPO_ROOT)))
        assert offenders == [], f"unguarded page.goto() in: {offenders}"


class TestApiTokenNotLogged:
    """The report review also turned up API tokens written to application logs."""

    def test_no_api_token_log_statements(self):
        offenders = []
        for py in (REPO_ROOT / "app").rglob("*.py"):
            if "tests" in py.parts or "__pycache__" in py.parts:
                continue
            for i, line in enumerate(py.read_text().splitlines(), 1):
                if "API TOKEN" in line and "logger" in line:
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{i}")
        assert offenders == [], f"api_token written to logs at: {offenders}"
