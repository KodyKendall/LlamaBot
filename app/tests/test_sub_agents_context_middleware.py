"""Sub-agents run the same context-management stack as the main agent.

``delegate_task`` / ``delegate_research`` build a bare ``create_agent`` with no
middleware at all: no summarization, no tool-output cap. A long delegated task
therefore grew without bound — the one path with nothing standing between a
``read_file`` of a huge file and the provider's context wall.
"""
import app.agents.leonardo.rails_agent.sub_agents as sub_agents
from app.agents.leonardo.summarization import RailsSummarizationMiddleware
from app.agents.leonardo.tool_output_middleware import ToolResultSizeLimitMiddleware


def _capture(monkeypatch):
    captured = []

    def fake_create_agent(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(sub_agents, "create_agent", fake_create_agent)
    monkeypatch.setattr(sub_agents, "get_llm", lambda name: object())
    return captured


def _assert_context_stack(kwargs, label):
    mws = kwargs.get("middleware") or []
    assert any(isinstance(m, RailsSummarizationMiddleware) for m in mws), (
        f"{label}: no summarization middleware — its context is unbounded"
    )
    assert any(isinstance(m, ToolResultSizeLimitMiddleware) for m in mws), (
        f"{label}: no tool-output cap — one huge read_file wedges it"
    )


def test_task_sub_agent_has_the_context_stack(monkeypatch):
    captured = _capture(monkeypatch)
    sub_agents.create_sub_agent("deepseek-v4-flash")
    assert len(captured) == 1
    _assert_context_stack(captured[0], "delegate_task")


def test_research_sub_agent_has_the_context_stack(monkeypatch):
    captured = _capture(monkeypatch)
    sub_agents.create_research_sub_agent("deepseek-v4-flash")
    assert len(captured) == 1
    _assert_context_stack(captured[0], "delegate_research")
