"""Regression coverage for cancellable, observable sub-agent delegation.

2026-08-23 (P1-5): the timeout itself was never the damage — the silence was.
`delegate_task` returned "Timed out after 240s — you may retry delegate_task
once" and nothing else, with no changed-file list, so the parent agent had to
re-read the tree or retry and edit the same files twice. 6 reports, 3 boxes,
three of them in one session on one box.

The run is streamed rather than `ainvoke`d for exactly this reason: whatever the
last completed step left behind is still in hand when the deadline fires.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.leonardo.delegation import (
    DelegationTimedOut,
    run_delegation,
    summarize_partial_work,
)


class _BlockingAgent:
    """Emits some state, then hangs — the shape of a real timeout."""

    def __init__(self, chunks=()):
        self.cancelled = False
        self.chunks = list(chunks)

    async def astream(self, input_data, config=None, stream_mode=None):
        for chunk in self.chunks:
            yield chunk
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _SuccessfulAgent:
    async def astream(self, input_data, config=None, stream_mode=None):
        yield {"messages": ["done"]}


@pytest.mark.asyncio
async def test_delegation_timeout_cancels_nested_agent_and_emits_terminal_progress():
    agent = _BlockingAgent()
    events = []

    with pytest.raises(DelegationTimedOut, match="timed out after 0.02s"):
        await run_delegation(
            agent,
            {"messages": [{"role": "user", "content": "research"}]},
            timeout_seconds=0.02,
            heartbeat_seconds=0.005,
            stream_writer=events.append,
            label="Research sub-agent",
        )

    assert agent.cancelled is True
    assert events[0]["phase"] == "started"
    assert any(event["phase"] == "working" for event in events)
    assert events[-1]["phase"] == "timed_out"
    assert all(event["type"] == "delegation_progress" for event in events)


@pytest.mark.asyncio
async def test_delegation_success_preserves_result_and_emits_completed():
    events = []

    result = await run_delegation(
        _SuccessfulAgent(),
        {"messages": [{"role": "user", "content": "research"}]},
        timeout_seconds=1,
        heartbeat_seconds=0.01,
        stream_writer=events.append,
        label="Research sub-agent",
        config={"metadata": {"thread_id": "thread-1"}},
    )

    assert result == {"messages": ["done"]}
    assert [event["phase"] for event in events] == ["started", "completed"]


@pytest.mark.asyncio
async def test_delegation_failure_emits_failed_and_reraises():
    class _FailingAgent:
        async def astream(self, input_data, config=None, stream_mode=None):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover

    events = []
    with pytest.raises(RuntimeError, match="provider exploded"):
        await run_delegation(
            _FailingAgent(),
            {"messages": []},
            timeout_seconds=1,
            stream_writer=events.append,
        )

    assert events[-1]["phase"] == "failed"


# ---------------------------------------------------------------------------
# The partial report
# ---------------------------------------------------------------------------

def _work_history():
    return [
        HumanMessage(content="add a Lead model with CRUD"),
        AIMessage(content="", tool_calls=[
            {"name": "write_file", "id": "c1",
             "args": {"file_path": "db/migrate/20260823_create_leads.rb", "content": "..."}},
        ]),
        ToolMessage(content="Updated file db/migrate/20260823_create_leads.rb", tool_call_id="c1"),
        AIMessage(content="", tool_calls=[
            {"name": "bash_command", "id": "c2",
             "args": {"command": "bin/rails db:migrate"}},
        ]),
        ToolMessage(content="Command output:\n== migrated", tool_call_id="c2"),
        AIMessage(content="", tool_calls=[
            {"name": "edit_file", "id": "c3",
             "args": {"file_path": "app/views/leads/index.html.erb",
                      "old_string": "x", "new_string": "y"}},
        ]),
        # No ToolMessage for c3 — this is where the deadline hit.
    ]


class TestSummarizePartialWork:
    def test_it_lists_the_files_it_touched(self):
        out = summarize_partial_work(_work_history())
        assert "db/migrate/20260823_create_leads.rb" in out

    def test_it_lists_the_commands_it_ran(self):
        out = summarize_partial_work(_work_history())
        assert "bin/rails db:migrate" in out

    def test_it_names_the_step_that_was_in_flight(self):
        out = summarize_partial_work(_work_history())
        assert "In progress" in out
        assert "app/views/leads/index.html.erb" in out

    def test_a_failed_call_is_flagged_so_the_parent_does_not_trust_it(self):
        history = [
            AIMessage(content="", tool_calls=[
                {"name": "edit_file", "id": "c1", "args": {"file_path": "app/models/x.rb"}},
            ]),
            ToolMessage(content="Error: Could not find old_string in file", tool_call_id="c1"),
        ]
        out = summarize_partial_work(history)
        assert "app/models/x.rb" in out
        assert "reported an error" in out

    def test_it_says_so_when_nothing_had_happened_yet(self):
        out = summarize_partial_work([HumanMessage(content="go")])
        assert "had not written any files" in out

    def test_it_survives_an_empty_history(self):
        assert summarize_partial_work([])
        assert summarize_partial_work(None)

    def test_it_is_bounded(self):
        history = []
        for i in range(200):
            history.append(AIMessage(content="", tool_calls=[
                {"name": "write_file", "id": f"c{i}", "args": {"file_path": f"app/models/m{i}.rb"}},
            ]))
            history.append(ToolMessage(content="Updated", tool_call_id=f"c{i}"))
        out = summarize_partial_work(history)
        assert "and 175 more" in out


@pytest.mark.asyncio
async def test_the_timeout_carries_the_partial_work_to_the_caller():
    agent = _BlockingAgent(chunks=[{"messages": _work_history()}])

    with pytest.raises(DelegationTimedOut) as excinfo:
        await run_delegation(
            agent, {"messages": []}, timeout_seconds=0.02, heartbeat_seconds=0.01,
        )

    report = summarize_partial_work(excinfo.value.partial_messages)
    assert "db/migrate/20260823_create_leads.rb" in report


@pytest.mark.asyncio
async def test_delegate_task_returns_the_report_instead_of_nothing(monkeypatch):
    """End to end through the tool the agent actually calls."""
    from app.agents.leonardo.rails_agent import sub_agents

    async def _timeout(*a, **k):
        raise DelegationTimedOut("timed out after 240s", partial_messages=_work_history())

    monkeypatch.setattr(sub_agents, "run_delegation", _timeout)
    monkeypatch.setattr(sub_agents, "create_sub_agent", lambda llm_model=None: object())

    class _Runtime:
        tool_call_id = "call_1"
        state = {"llm_model": "deepseek-v4-flash"}
        config = {}
        stream_writer = None

    command = await sub_agents.delegate_task.coroutine(
        task_description="build it", runtime=_Runtime(),
    )
    content = command.update["messages"][0].content

    assert "db/migrate/20260823_create_leads.rb" in content
    assert "do NOT redo it" in content
