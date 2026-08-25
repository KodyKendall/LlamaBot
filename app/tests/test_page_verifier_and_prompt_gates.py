"""P0 (2026-08-23): Leo was told to self-verify pages with a tool that is off by default.

`browser_inspect` is gated by the `enable_browser_inspect` site setting, which
defaults to "false", but both Rails prompts documented it unconditionally. On a
default box the agent called it and got
`Error: browser_inspect is not a valid tool` — 8 friction reports across 7 boxes
— and then shipped the page unverified. The customer-app error stream is
dominated by failures one page load catches instantly: NoMethodError (16 boxes),
NameError (13), ActionView::SyntaxErrorInTemplate (10), 422 occurrences in 7 days.

Three properties are asserted here:

1. A gated section never reaches the model when its gate is off, and is
   untouched when it is on — for EVERY prompt, via the shared choke point.
2. `check_page`, the always-available verifier, is bound in every Rails mode
   that can write files, on a default box with the setting unset.
3. `check_page` reports the truth: 2xx tiny, 3xx as "not proven", non-2xx with
   the exception off the Rails log.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from app.agents.leonardo.prompt_gates import GATES, apply_prompt_gates

LEONARDO_DIR = Path(__file__).resolve().parents[1] / "agents" / "leonardo"

# Every mode that can write a view/controller/route, and therefore has to be
# able to check what it wrote. rails_user_feedback_agent and rails_user_mode_agent
# deliberately have no write tools; rails_plain_chat_mode has no tools at all.
BUILDING_MODES = [
    "rails_agent",
    "rails_ai_builder_agent",
    "rails_beginner_agent",
    "rails_engineer_plan_mode_agent",
    "rails_plan_mode_agent",
    "rails_testing_agent",
    "rails_ticket_mode_agent",
    "rails_ticket_plan_mode_agent",
]


# ---------------------------------------------------------------------------
# 1. Prompt gates
# ---------------------------------------------------------------------------

SAMPLE = (
    "intro line\n"
    "<!--IF:browser_inspect-->\n"
    "### Self-Checking Pages with browser_inspect\n"
    "call browser_inspect after every view edit\n"
    "<!--END:browser_inspect-->\n"
    "closing line\n"
)


class TestApplyPromptGates:
    def test_closed_gate_removes_the_whole_block(self, monkeypatch):
        monkeypatch.setitem(GATES, "browser_inspect", lambda: False)
        out = apply_prompt_gates(SAMPLE)
        assert "browser_inspect" not in out
        assert "intro line" in out and "closing line" in out

    def test_open_gate_keeps_the_content_and_drops_the_markers(self, monkeypatch):
        monkeypatch.setitem(GATES, "browser_inspect", lambda: True)
        out = apply_prompt_gates(SAMPLE)
        assert "call browser_inspect after every view edit" in out
        assert "<!--IF:" not in out and "<!--END:" not in out

    def test_an_unreadable_gate_fails_closed(self, monkeypatch):
        """The tool list fails closed too — the two must never disagree."""
        def _boom():
            raise RuntimeError("auth DB down")

        monkeypatch.setitem(GATES, "browser_inspect", _boom)
        assert "browser_inspect" not in apply_prompt_gates(SAMPLE)

    def test_an_unknown_gate_is_left_alone(self):
        text = "a\n<!--IF:some_future_tool-->\nkeep me\n<!--END:some_future_tool-->\nb\n"
        assert "keep me" in apply_prompt_gates(text)

    def test_prompts_without_markers_are_returned_untouched(self):
        text = "just a normal prompt"
        assert apply_prompt_gates(text) is text


class TestGatesAreWiredToTheRealPrompts:
    """The whole point: the LIVE prompt, through the real choke point."""

    def _prompt(self, mode, base, enabled, cached=None):
        from app.agents.leonardo import project_context

        # Isolate from whatever the mothership has cached for this box: we are
        # asserting what the SOURCE prompt resolves to. The override path gets
        # its own test below — it is gated too, and on a box where an override
        # exists it is the only prompt that runs.
        with patch.dict(GATES, {"browser_inspect": lambda: enabled}), \
             patch.object(project_context.system_prompt_cache, "get_cached",
                          return_value=cached):
            return project_context.resolve_base_prompt(base, mode)

    def test_a_mothership_override_is_gated_too(self):
        """A delivered override is the ONLY prompt that runs on that box."""
        override = "x" * 3000 + SAMPLE
        off = self._prompt("rails_agent", "static", enabled=False, cached=override)
        on = self._prompt("rails_agent", "static", enabled=True, cached=override)
        assert "browser_inspect" not in off
        assert "browser_inspect" in on

    @pytest.mark.parametrize(
        "module,const",
        [
            ("rails_beginner_agent", "BEGINNER_AGENT_PROMPT"),
            ("rails_agent", "RAILS_AGENT_PROMPT"),
        ],
    )
    def test_browser_inspect_is_absent_when_the_setting_is_off(self, module, const):
        import importlib

        prompts = importlib.import_module(f"app.agents.leonardo.{module}.prompts")
        base = getattr(prompts, const)
        assert "browser_inspect" in base, "fixture drifted: prompt no longer mentions it"

        out = self._prompt(module, base, enabled=False)
        assert "browser_inspect" not in out, (
            f"{module} still advertises browser_inspect on a default box, so the "
            f"agent will call a tool the registry does not have"
        )

    @pytest.mark.parametrize(
        "module,const",
        [
            ("rails_beginner_agent", "BEGINNER_AGENT_PROMPT"),
            ("rails_agent", "RAILS_AGENT_PROMPT"),
        ],
    )
    def test_browser_inspect_is_present_when_the_setting_is_on(self, module, const):
        import importlib

        prompts = importlib.import_module(f"app.agents.leonardo.{module}.prompts")
        out = self._prompt(module, getattr(prompts, const), enabled=True)
        assert "browser_inspect" in out
        assert "<!--IF:" not in out

    @pytest.mark.parametrize(
        "module,const",
        [
            ("rails_beginner_agent", "BEGINNER_AGENT_PROMPT"),
            ("rails_agent", "RAILS_AGENT_PROMPT"),
        ],
    )
    def test_the_always_on_verifier_is_documented_either_way(self, module, const):
        import importlib

        prompts = importlib.import_module(f"app.agents.leonardo.{module}.prompts")
        base = getattr(prompts, const)
        for enabled in (True, False):
            assert "check_page" in self._prompt(module, base, enabled=enabled)


def test_no_prompt_mentions_a_gated_tool_outside_a_gate():
    """The bug class, not the one instance."""
    import re

    offenders = []
    for prompts_file in sorted(LEONARDO_DIR.glob("*/prompts.py")):
        text = prompts_file.read_text()
        for gate in GATES:
            if gate not in text:
                continue
            stripped = re.sub(
                r"<!--\s*IF:%s\s*-->.*?<!--\s*END:%s\s*-->" % (gate, gate),
                "", text, flags=re.DOTALL,
            )
            if gate in stripped:
                offenders.append(f"{prompts_file.parent.name}: {gate}")
    assert not offenders, (
        "these prompts describe a tool that is gated off by default, outside a "
        f"<!--IF:...--> block: {offenders}"
    )


# ---------------------------------------------------------------------------
# 2. check_page is bound everywhere it is needed
# ---------------------------------------------------------------------------

def _tool_names(tools):
    return {getattr(t, "name", getattr(t, "__name__", str(t))) for t in tools}


@pytest.mark.parametrize("mode", BUILDING_MODES)
def test_check_page_is_bound_in_every_building_mode(mode):
    """On a default box, with browser_inspect off, the agent still has a verifier."""
    import importlib

    nodes = importlib.import_module(f"app.agents.leonardo.{mode}.nodes")
    names = _tool_names(nodes.default_tools)
    assert "check_page" in names, (
        f"{mode} can write views but has no way to load one — this is how a "
        f"NoMethodError reaches the customer instead of the agent"
    )


def test_beginner_binds_check_page_to_the_model_not_just_the_toolnode():
    """Beginner mode keeps a second list for what the MODEL sees; a tool in only
    one of them is executable but invisible."""
    from app.agents.leonardo.rails_beginner_agent.nodes import beginner_turn_tools

    assert "check_page" in _tool_names(beginner_turn_tools())


# ---------------------------------------------------------------------------
# 3. check_page behaviour
# ---------------------------------------------------------------------------

RAILS_LOG = """
Started GET "/comments" for 172.18.0.1 at 2026-08-23 01:00:00 +0000
Processing by CommentsController#index as HTML
  Rendering comments/index.html.erb

NoMethodError (undefined method `any?' for nil):

app/views/comments/index.html.erb:67:in `_app_views_comments_index_html_erb'
app/controllers/comments_controller.rb:8:in `index'
  actionview (7.1.0) lib/action_view/template.rb:262:in `block in render'
  actionpack (7.1.0) lib/action_controller/metal/instrumentation.rb:69:in `render'
"""


class TestExtractRailsException:
    def test_it_finds_the_class_message_and_app_frames(self):
        from app.agents.leonardo.rails_agent.tools import _rails_exception_from_logs

        out = _rails_exception_from_logs(RAILS_LOG)
        assert out.startswith("NoMethodError: undefined method `any?' for nil")
        assert "app/views/comments/index.html.erb:67" in out

    def test_it_stops_before_the_framework_frames(self):
        from app.agents.leonardo.rails_agent.tools import _rails_exception_from_logs

        out = _rails_exception_from_logs(RAILS_LOG)
        assert "actionview" not in out and "actionpack" not in out

    def test_it_returns_none_on_a_clean_log(self):
        from app.agents.leonardo.rails_agent.tools import _rails_exception_from_logs

        assert _rails_exception_from_logs('Started GET "/" for 1.2.3.4\nCompleted 200 OK\n') is None

    def test_it_takes_the_most_recent_exception(self):
        from app.agents.leonardo.rails_agent.tools import _rails_exception_from_logs

        log = RAILS_LOG + "\nNameError (undefined local variable or method `foo'):\n\napp/controllers/x.rb:3:in `index'\n"
        assert _rails_exception_from_logs(log).startswith("NameError")


class _Runtime:
    tool_call_id = "call_1"


class _Response:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def _invoke_check_page(monkeypatch, *, response=None, error=None, log=""):
    import httpx

    import app.agents.leonardo.rails_agent.tools as tools

    def _get(url, **kwargs):
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(tools, "_tail_rails_log_text", lambda *a, **k: log)
    command = tools.check_page.func(path="/comments", runtime=_Runtime())
    return command.update["messages"][0].content


class TestCheckPage:
    def test_a_2xx_answer_is_short_and_says_it_rendered(self, monkeypatch):
        out = _invoke_check_page(monkeypatch, response=_Response(200))
        assert "200" in out and "rendered" in out
        assert len(out) < 200, "this runs after every view edit; it must stay tiny"

    def test_a_500_reports_the_exception_from_the_log(self, monkeypatch):
        out = _invoke_check_page(monkeypatch, response=_Response(500), log=RAILS_LOG)
        assert "500" in out
        assert "NoMethodError" in out
        assert "app/views/comments/index.html.erb:67" in out

    def test_a_redirect_is_reported_as_not_proven_rather_than_broken(self, monkeypatch):
        out = _invoke_check_page(
            monkeypatch, response=_Response(302, {"location": "/login"})
        )
        assert "302" in out and "/login" in out
        assert "did NOT render" not in out

    def test_a_500_with_a_clean_log_still_says_something_useful(self, monkeypatch):
        out = _invoke_check_page(monkeypatch, response=_Response(404), log="")
        assert "404" in out
        assert "routes.rb" in out

    def test_an_unreachable_app_is_a_reported_result_not_a_crash(self, monkeypatch):
        out = _invoke_check_page(
            monkeypatch, error=ConnectionError("connection refused"), log=RAILS_LOG
        )
        assert "Could not reach" in out
        assert "NoMethodError" in out

    def test_the_output_is_capped(self, monkeypatch):
        from app.agents.leonardo.rails_agent.tools import CHECK_PAGE_MAX_CHARS

        huge = "\n".join(
            ["RuntimeError (boom):", ""] + [f"app/models/x{i}.rb:{i}:in `go'" for i in range(500)]
        )
        out = _invoke_check_page(monkeypatch, response=_Response(500), log=huge)
        assert len(out) <= CHECK_PAGE_MAX_CHARS + 200

    def test_it_targets_the_url_that_works_inside_the_container(self):
        from app.agents.leonardo.rails_agent.tools import _check_page_url

        assert _check_page_url("/leads") == "http://llamapress:3000/leads"
        assert _check_page_url("leads") == "http://llamapress:3000/leads"

    def test_it_refuses_a_url_outside_the_app(self, monkeypatch):
        import app.agents.leonardo.rails_agent.tools as tools

        command = tools.check_page.func(
            path="http://llamabot:8000/api/site_settings", runtime=_Runtime()
        )
        content = command.update["messages"][0].content
        assert "Refused" in content
