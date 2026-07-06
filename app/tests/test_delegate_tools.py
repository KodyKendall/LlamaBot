from types import SimpleNamespace

import pytest

from app.agents.leonardo.delegation import DelegationTimedOut


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "failure_marker"),
    [
        ("app.agents.leonardo.rails_agent.sub_agents", "[DELEGATED TASK FAILED]"),
        ("app.agents.leonardo.rails_ticket_mode_agent.sub_agents", "[DELEGATED TASK FAILED]"),
        ("app.agents.leonardo.rails_user_feedback_agent.sub_agents", "[DELEGATED RESEARCH FAILED]"),
    ],
)
async def test_delegate_task_timeout_returns_recoverable_tool_message(
    monkeypatch, module_name, failure_marker
):
    module = __import__(module_name, fromlist=["sub_agents"])

    async def time_out(*args, **kwargs):
        raise DelegationTimedOut("timed out after 240s")

    monkeypatch.setattr(module, "create_sub_agent", lambda **_: object())
    monkeypatch.setattr(module, "run_delegation", time_out)
    runtime = SimpleNamespace(
        tool_call_id="call-1",
        state={"llm_model": "deepseek-v4-flash"},
        config={"configurable": {"thread_id": "thread-1"}},
        stream_writer=lambda _: None,
    )

    command = await module.delegate_task.coroutine(
        task_description="research the issue", runtime=runtime
    )

    assert command.update["failed_tool_calls_count"] == 1
    message = command.update["messages"][0]
    assert failure_marker in message.content
    assert "Timed out after 240s" in message.content
    assert "retry" in message.content
    assert message.tool_call_id == "call-1"
