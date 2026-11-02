"""Ask-before-edits mode must pause before anything that can change the project.

The rails_agent's approval list only named edit_file, write_file and bash_command.
delegate_task spawns a sub-agent with full write + shell access, and
delegate_research's "read-only" sub-agent carried bash_command, so in ask mode both
reached files and the shell without the user ever being asked.

These tests drive the real approval middleware config through a small create_agent
graph (build_workflow itself is not called: it clears the asyncio loop in CI).
"""

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.leonardo.rails_agent import nodes, sub_agents


class _Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.parametrize(
    "tool_name",
    ["edit_file", "write_file", "bash_command",
     "delegate_task", "delegate_research", "hard_restart_rails", "fix_permissions"],
)
def test_ask_before_edits_lists_every_destructive_tool(tool_name):
    assert tool_name in nodes.ASK_BEFORE_EDITS_TOOLS


def test_every_listed_tool_exists_on_the_agent():
    # A typo in the list silently gates nothing.
    names = {t.name for t in nodes.default_tools}
    assert set(nodes.ASK_BEFORE_EDITS_TOOLS) <= names


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["delegate_task", "delegate_research"])
async def test_agent_pauses_for_approval_before_delegating(tool_name):
    ran = []

    @tool(tool_name)
    async def delegate(task_description: str) -> str:
        """Spawn a sub-agent."""
        ran.append(task_description)
        return "sub-agent finished"

    agent = create_agent(
        _Model(responses=[
            AIMessage(content="", tool_calls=[
                {"name": tool_name, "id": "d1", "args": {"task_description": "change things"}},
            ]),
            AIMessage(content="done"),
        ]),
        tools=[delegate],
        middleware=[nodes.ask_before_edits_middleware()],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": f"ask-{tool_name}"}}
    result = await agent.ainvoke({"messages": [("user", "go")]}, config)

    assert ran == [], "sub-agent ran before the user approved"
    interrupts = result.get("__interrupt__") or []
    assert interrupts, "no approval request was raised"
    requests = interrupts[0].value["action_requests"]
    assert [r["name"] for r in requests] == [tool_name]


def test_research_sub_agent_has_no_shell():
    names = {t.name for t in sub_agents.RESEARCH_TOOLS}
    assert "bash_command" not in names
    assert not names & {"write_file", "edit_file", "git_commit", "git_command",
                        "delegate_task", "delegate_research"}


def test_research_sub_agent_can_read_uploaded_files_without_a_shell():
    # With no shell, read_uploaded_file is how research opens an uploaded xlsx/csv.
    names = {t.name for t in sub_agents.RESEARCH_TOOLS}
    assert "read_uploaded_file" in names
    assert "bash_command" not in names
    assert "cannot open spreadsheets" not in sub_agents.RESEARCH_ONLY_PROMPT
    assert "read_uploaded_file" in sub_agents.RESEARCH_ONLY_PROMPT


def test_research_prompt_and_docs_do_not_promise_a_shell():
    assert "bash_command" not in sub_agents.RESEARCH_ONLY_PROMPT
    assert "rails runner" not in (sub_agents.delegate_research.description or "")


def test_auto_mode_adds_no_approval_gate():
    # Auto mode must delegate (and edit) without asking.
    assert nodes.approval_middleware(False) == []


def test_ask_mode_adds_the_approval_gate():
    [mw] = nodes.approval_middleware(True)
    assert {"delegate_task", "delegate_research"} <= set(mw.interrupt_on)
