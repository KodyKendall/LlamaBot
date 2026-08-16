"""No single tool result may be big enough to wedge a thread.

The incident (2026-08-13, mothership thread 5b4ab08e): the agent grepped for
``confirm\\(`` and one match landed inside a **minified** vendor bundle whose
longest line is 289,158 characters. ripgrep printed that whole line, and
``grep_files`` — which caps by LINE COUNT and has no byte cap — handed back a
single ``ToolMessage`` of 294,807 characters (~74k tokens).

A message that big is permanently uncompactable: the preserved recent tail is
30k tokens and ``SummarizationMiddleware`` never splits a tool-call group, so it
survives every compaction. The thread re-summarized forever and the user saw a
spinner for 18 minutes.

Two layers are asserted here:

- Fix 1 — ``grep_files`` specifically, in BYTES. A line-count assertion is
  exactly what let this ship, so every assertion below is on ``len()``.
- Fix 2 — the class. ``cap_tool_result`` bounds ANY tool result, including the
  tools nobody has written yet, and is wired into both agent shapes
  (``create_agent`` middleware and the raw ``ToolNode`` graphs).
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.leonardo.rails_agent import tools
from app.agents.utils.tool_output_limits import (
    TOOL_RESULT_MAX_BYTES,
    TOOL_RESULT_TRUNCATION_NOTICE,
    cap_tool_message,
    cap_tool_result,
    cap_tool_node_output,
)


# The real xterm.js line that caused the incident, rounded down.
MINIFIED_LINE_CHARS = 289_158


class _Runtime:
    """Stand-in for the injected ToolRuntime — the tool reads only this."""

    def __init__(self, tool_call_id="call-1"):
        self.tool_call_id = tool_call_id


def _message_text(command) -> str:
    """Pull the single ToolMessage's text out of a tool's Command update."""
    messages = command.update["messages"]
    assert len(messages) == 1
    return messages[0].content


@pytest.fixture
def rails_root(tmp_path, monkeypatch):
    """A throwaway Rails root containing a realistic minified bundle."""
    root = tmp_path / "rails"
    (root / "app" / "javascript" / "vendor" / "xterm").mkdir(parents=True)
    (root / "app" / "models").mkdir(parents=True)

    # One 289k-char line with the pattern inside it — the exact shape that bit us.
    filler = "a" * (MINIFIED_LINE_CHARS // 2)
    (root / "app" / "javascript" / "vendor" / "xterm" / "xterm.js").write_text(
        f"{filler}window.confirm(1){filler}\n"
    )
    # The same shape OUTSIDE the ignore globs, so the byte cap is exercised even
    # when the glob happens to save us. Long single lines are not only vendored:
    # generated seed data and compiled templates look like this too.
    (root / "app" / "models" / "seed_data.rb").write_text(
        f"SEED = %q({filler}confirm(1){filler})\n"
    )
    (root / "app" / "models" / "user.rb").write_text(
        "class User\n  def ask\n    confirm(:yes)\n  end\nend\n"
    )

    monkeypatch.setattr(tools, "RAILS_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# Fix 1 — grep_files caps by bytes
# ---------------------------------------------------------------------------

def test_grep_files_output_is_bounded_in_bytes(rails_root):
    """The regression test for the incident, asserted in bytes not lines."""
    command = tools.grep_files.func(
        pattern=r"confirm\(", runtime=_Runtime(), path="app"
    )
    text = _message_text(command)

    assert len(text) <= TOOL_RESULT_MAX_BYTES
    # And comfortably under it: this is a 2-file search, not a 74k-token dump.
    assert len(text) < tools.BASH_OUTPUT_MAX_CHARS * 2
    # The 289k-char line must never appear whole.
    assert MINIFIED_LINE_CHARS // 2 > len(text)


def test_grep_files_still_reports_the_long_line_matched(rails_root):
    """Truncating must not hide the match — the agent still learns the file hit."""
    command = tools.grep_files.func(
        pattern=r"confirm\(", runtime=_Runtime(), path="app"
    )
    text = _message_text(command)

    assert "user.rb" in text
    assert "confirm(:yes)" in text


def test_grep_files_skips_vendored_and_minified_files(rails_root):
    """Compiled/vendored assets are never what a code search wants."""
    (rails_root / "app" / "assets").mkdir(parents=True)
    (rails_root / "app" / "assets" / "app.min.js").write_text(
        "x" * 50_000 + "confirm(9)\n"
    )
    command = tools.grep_files.func(
        pattern=r"confirm\(", runtime=_Runtime(), path="app"
    )
    text = _message_text(command)

    assert "app.min.js" not in text
    assert "xterm.js" not in text


def test_grep_files_result_survives_many_moderate_matches(rails_root):
    """A normal search with lots of hits is still capped, and still useful."""
    big = rails_root / "app" / "models"
    for i in range(200):
        (big / f"m{i}.rb").write_text("confirm(:a)\n" * 40)

    command = tools.grep_files.func(
        pattern=r"confirm\(", runtime=_Runtime(), path="app"
    )
    text = _message_text(command)

    assert len(text) <= TOOL_RESULT_MAX_BYTES
    assert "confirm(:a)" in text


# ---------------------------------------------------------------------------
# Fix 2 — the class: one cap every tool result passes through
# ---------------------------------------------------------------------------

def test_cap_tool_result_leaves_normal_output_untouched():
    text = "app/models/user.rb:12:  validates :email\n" * 10
    assert cap_tool_result(text, tool_name="grep_files") is text


def test_cap_tool_result_bounds_anything_oversized():
    capped = cap_tool_result("z" * 500_000, tool_name="some_future_tool")
    assert len(capped.encode()) <= TOOL_RESULT_MAX_BYTES


def test_cap_tool_result_keeps_head_and_tail_and_says_so():
    """The agent must be able to tell partial output from broken output."""
    body = "HEAD-MARKER\n" + ("z" * 500_000) + "\nTAIL-MARKER"
    capped = cap_tool_result(body, tool_name="bash_command")

    assert "HEAD-MARKER" in capped
    assert "TAIL-MARKER" in capped
    assert TOOL_RESULT_TRUNCATION_NOTICE.strip().splitlines()[0] in capped


def test_cap_tool_message_rewrites_the_message_in_place():
    msg = ToolMessage(content="z" * 400_000, tool_call_id="abc", name="grep_files")
    capped = cap_tool_message(msg)

    assert len(capped.content.encode()) <= TOOL_RESULT_MAX_BYTES
    # Identity must survive or the AI/Tool pair breaks and the turn won't run.
    assert capped.tool_call_id == "abc"
    assert capped.name == "grep_files"


def test_cap_tool_message_preserves_small_messages_identically():
    msg = ToolMessage(content="ok", tool_call_id="abc")
    assert cap_tool_message(msg) is msg


def test_cap_tool_message_caps_text_blocks_but_not_media():
    """A truncated base64 image is a broken image; only text blocks shrink."""
    msg = ToolMessage(
        content=[
            {"type": "text", "text": "z" * 400_000},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        tool_call_id="abc",
    )
    capped = cap_tool_message(msg)

    text_blocks = [b for b in capped.content if b.get("type") == "text"]
    assert len(text_blocks[0]["text"].encode()) <= TOOL_RESULT_MAX_BYTES
    assert capped.content[1] == msg.content[1]


def test_cap_tool_message_ignores_non_tool_messages():
    ai = AIMessage(content="z" * 400_000)
    assert cap_tool_message(ai) is ai


# -- the choke point applies to the raw-ToolNode agents too -----------------

def test_cap_tool_node_output_handles_the_messages_dict_shape():
    out = {"messages": [ToolMessage(content="z" * 400_000, tool_call_id="a")]}
    capped = cap_tool_node_output(out)
    assert len(capped["messages"][0].content.encode()) <= TOOL_RESULT_MAX_BYTES


def test_cap_tool_node_output_handles_command_updates():
    from langgraph.types import Command

    out = [Command(update={"messages": [ToolMessage(content="z" * 400_000, tool_call_id="a")]})]
    capped = cap_tool_node_output(out)
    assert len(capped[0].update["messages"][0].content.encode()) <= TOOL_RESULT_MAX_BYTES


def test_cap_tool_node_output_passes_through_unknown_shapes():
    sentinel = object()
    assert cap_tool_node_output(sentinel) is sentinel


def test_capped_tool_node_is_a_real_tool_node():
    """It has to remain a Runnable or `builder.add_node` rejects it."""
    from langgraph.prebuilt import ToolNode

    from app.agents.utils.tool_output_limits import CappedToolNode

    assert issubclass(CappedToolNode, ToolNode)


@pytest.mark.parametrize("method", ["invoke", "ainvoke"])
def test_capped_tool_node_bounds_what_the_tool_node_returns(method):
    """Whatever ToolNode hands back, the graph only ever sees a bounded result."""
    import asyncio
    from unittest.mock import patch

    from langgraph.prebuilt import ToolNode

    from app.agents.utils.tool_output_limits import CappedToolNode

    firehose = {"messages": [ToolMessage(content="z" * 400_000, tool_call_id="t1")]}

    node = CappedToolNode.__new__(CappedToolNode)
    if method == "invoke":
        with patch.object(ToolNode, "invoke", return_value=firehose):
            out = node.invoke({"messages": []})
    else:
        async def _fake(self, *a, **kw):
            return firehose

        with patch.object(ToolNode, "ainvoke", _fake):
            out = asyncio.run(node.ainvoke({"messages": []}))

    assert len(out["messages"][0].content.encode()) <= TOOL_RESULT_MAX_BYTES


# -- and it is impossible to skip -------------------------------------------

def test_every_agent_registers_a_capped_tool_node():
    """No live agent may wire a bare ToolNode as its tools node.

    This is the structural half of the fix. Three incidents in a row came from a
    cap that existed but wasn't applied everywhere, so the "every tool result is
    bounded" claim is asserted over the source rather than trusted.
    """
    import pathlib
    import re

    agents_dir = pathlib.Path(__file__).resolve().parent.parent / "agents"
    # `nodes.old.py` is dead code; `prompts.py` contains an example graph inside
    # a prompt string, not a graph this process ever builds.
    skip = {"nodes.old.py", "prompts.py"}

    offenders = []
    for path in agents_dir.rglob("*.py"):
        if path.name in skip:
            continue
        for line in path.read_text().splitlines():
            if re.search(r'add_node\(\s*["\']tools["\']\s*,\s*ToolNode\(', line):
                offenders.append(f"{path.relative_to(agents_dir)}: {line.strip()}")

    assert not offenders, (
        "These agents register an uncapped ToolNode — use CappedToolNode so a "
        "single huge tool result can't wedge the thread:\n" + "\n".join(offenders)
    )


def test_every_create_agent_gets_the_size_limit_middleware():
    """The `create_agent` half: added centrally so a new mode can't forget it."""
    from unittest.mock import patch

    from app.agents.leonardo import agent_factory
    from app.agents.leonardo.tool_output_middleware import ToolResultSizeLimitMiddleware

    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return "agent"

    with patch.object(agent_factory, "create_agent", fake_create_agent):
        agent_factory.build_leonardo_agent(model="x", tools=[])

    assert any(
        isinstance(m, ToolResultSizeLimitMiddleware) for m in captured["middleware"]
    )
