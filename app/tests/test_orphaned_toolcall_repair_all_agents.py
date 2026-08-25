"""Backstop tests for orphaned-tool-call repair across ALL Leonardo agents.

SupportIncident #112: `RepairOrphanedToolCallsMiddleware` was wired into
`rails_agent` ONLY. Every other agent graph was unprotected, so an
interrupted/crashed tool call (e.g. answering an `ask_user_question` interrupt
with plain chat in Plan mode) left an `AIMessage` with `tool_calls` and no
matching `ToolMessage` — and every later turn hit a hard
`400 insufficient tool messages`.

These tests assert three things:
1. The pure repair function does the right thing (behavior).
2. `build_leonardo_agent` always prepends the repair middleware (the create_agent path).
3. STRUCTURAL backstop: every Leonardo agent `nodes.py` is wired for repair — so a
   NEW agent added later that forgets the wiring fails the suite. This is the real
   guarantee (it catches the whole class), and it's intentionally a static scan so
   it doesn't have to build graphs (build_workflow() clears the asyncio loop — see
   the team's CI memo on that landmine).
"""

import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.leonardo.agent_factory import (
    RepairOrphanedToolCallsMiddleware,
    build_leonardo_agent,
    repair_orphaned_tool_calls_in_messages,
)

LEONARDO_DIR = Path(__file__).resolve().parents[1] / "agents" / "leonardo"

# Leonardo graphs registered in app/langgraph.json (llamabot/llamapress are a
# different agent/prompt mechanism and intentionally out of scope for SI#112).
LEONARDO_GRAPH_KEYS = [
    "rails_agent",
    "rails_ai_builder_agent",
    "rails_testing_agent",
    "rails_ticket_mode_agent",
    "rails_ticket_plan_mode_agent",
    "rails_user_mode_agent",
    "rails_beginner_agent",
    "rails_plan_mode_agent",
    "rails_engineer_plan_mode_agent",
    "pyxl_agent",
]


def _orphan_history():
    """An AIMessage with a tool_call and no following ToolMessage — the exact
    shape that 400s the provider on the next turn."""
    return [
        HumanMessage(content="please run the migration"),
        AIMessage(
            content="",
            tool_calls=[{"id": "call_1", "name": "bash_command", "args": {}}],
        ),
    ]


# --------------------------------------------------------------------------
# 1. Pure repair function behaviour
# --------------------------------------------------------------------------

def test_repair_injects_placeholder_for_orphan():
    msgs = _orphan_history()
    out = repair_orphaned_tool_calls_in_messages(msgs)

    assert out is not msgs  # changed -> new list
    tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"

    # placeholder lands immediately after the AIMessage that owns the tool_call
    ai_idx = next(i for i, m in enumerate(out) if isinstance(m, AIMessage))
    assert isinstance(out[ai_idx + 1], ToolMessage)


def test_repair_is_noop_when_tool_call_already_answered():
    msgs = [
        AIMessage(content="", tool_calls=[{"id": "call_1", "name": "x", "args": {}}]),
        ToolMessage(content="done", tool_call_id="call_1"),
    ]
    out = repair_orphaned_tool_calls_in_messages(msgs)
    assert out is msgs  # unchanged -> same object (idempotent / cheap no-op signal)


def test_repair_handles_multiple_orphans_in_one_message():
    msgs = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "a", "name": "x", "args": {}},
                {"id": "b", "name": "y", "args": {}},
            ],
        ),
    ]
    out = repair_orphaned_tool_calls_in_messages(msgs)
    ids = {m.tool_call_id for m in out if isinstance(m, ToolMessage)}
    assert ids == {"a", "b"}


def test_repair_noop_without_tool_calls():
    msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
    assert repair_orphaned_tool_calls_in_messages(msgs) is msgs


# --------------------------------------------------------------------------
# 2. Middleware + factory wiring
# --------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, messages):
        self.messages = messages
        self.overridden = None

    def override(self, messages):
        self.overridden = messages
        return self


def test_middleware_repairs_via_wrap_model_call():
    mw = RepairOrphanedToolCallsMiddleware()
    req = _FakeRequest(_orphan_history())
    mw.wrap_model_call(req, lambda r: "ok")
    assert req.overridden is not None
    assert any(isinstance(m, ToolMessage) for m in req.overridden)


def test_factory_prepends_repair_when_absent(monkeypatch):
    import app.agents.leonardo.agent_factory as af
    captured = {}
    monkeypatch.setattr(af, "create_agent", lambda **kw: captured.update(kw) or "AGENT")

    af.build_leonardo_agent(model="m", tools=[])
    mw = captured["middleware"]
    assert isinstance(mw[0], af.RepairOrphanedToolCallsMiddleware)  # PREPENDED, first


def test_factory_is_idempotent_when_repair_already_present(monkeypatch):
    import app.agents.leonardo.agent_factory as af
    captured = {}
    monkeypatch.setattr(af, "create_agent", lambda **kw: captured.update(kw) or "AGENT")

    existing = af.RepairOrphanedToolCallsMiddleware()
    af.build_leonardo_agent(model="m", tools=[], middleware=[existing, "other"])
    mw = captured["middleware"]
    assert sum(isinstance(m, af.RepairOrphanedToolCallsMiddleware) for m in mw) == 1
    assert mw[0] is existing  # not duplicated; original order preserved


# --------------------------------------------------------------------------
# 3. Structural backstop — every Leonardo agent must be wired for repair
# --------------------------------------------------------------------------

def _agent_nodes_files():
    return sorted(LEONARDO_DIR.glob("*/nodes.py"))


@pytest.mark.parametrize(
    "nodes_file", _agent_nodes_files(), ids=lambda p: p.parent.name
)
def test_every_leonardo_agent_is_wired_for_repair(nodes_file):
    """A new agent that forgets the cross-cutting repair wiring fails here."""
    text = nodes_file.read_text()

    uses_factory = "build_leonardo_agent(" in text
    uses_raw_create_agent = bool(re.search(r"\bcreate_agent\(", text))
    uses_stategraph = "StateGraph(" in text and ".invoke(" in text

    if not (uses_factory or uses_raw_create_agent or uses_stategraph):
        pytest.skip(f"{nodes_file.parent.name} does not construct an agent")

    # An agent that binds NO tools cannot produce an orphaned tool call, so it has
    # nothing to repair (SI#112 is specifically about AIMessage.tool_calls with no
    # matching ToolMessage). Deliberately narrow: bind a tool by any of the usual
    # routes and the backstop applies again — this exempts genuinely tool-less
    # agents, not agents that merely wire tools in an unusual way.
    #
    # Scan CODE only: a comment saying "no bind_tools() here" must not read as
    # binding tools.
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    binds_tools = any(s in code for s in ("bind_tools(", "ToolNode(", "tools=", "tool_list"))
    if not binds_tools:
        pytest.skip(f"{nodes_file.parent.name} binds no tools — no orphans possible")

    # create_agent path: must go through the factory (which prepends repair).
    if uses_factory:
        return
    if uses_raw_create_agent:
        pytest.fail(
            f"{nodes_file.parent.name} calls create_agent() directly — use "
            f"build_leonardo_agent() so orphaned tool calls are repaired (SI#112)."
        )

    # raw StateGraph path: must run the messages through the shared validator
    # before .invoke(). `normalize_messages_for_provider` is the single entry
    # point (2026-08-23); it owns tool-call pairing plus the other invariants the
    # providers enforce. The older `repair_orphaned_tool_calls_in_messages` is
    # still accepted — it is the same rule, one layer down.
    assert (
        "normalize_messages_for_provider" in text
        or "repair_orphaned_tool_calls_in_messages" in text
    ), (
        f"{nodes_file.parent.name} is a raw StateGraph agent but never runs its "
        f"messages through normalize_messages_for_provider() before .invoke() "
        f"(SI#112 + the 2026-08-23 malformed-history set)."
    )


def test_scan_covers_all_registered_leonardo_graphs():
    """Guards the scan above: every graph in langgraph.json has a nodes.py the
    parametrized test actually visited."""
    present = {p.parent.name for p in _agent_nodes_files()}
    missing = [k for k in LEONARDO_GRAPH_KEYS if k not in present]
    assert not missing, f"registered Leonardo graphs missing a nodes.py: {missing}"


# ---------------------------------------------------------------------------
# Repair layer 2 — the write has to be ACCEPTED, not just attempted
# ---------------------------------------------------------------------------

class TestRepairSurvivesAMiddlewareLastWriter:
    """`aupdate_state` defaults `as_node` to the last checkpoint writer.

    When that writer was summarization, the name LangGraph finds is
    `RailsSummarizationMiddleware.before_model` — a middleware HOOK, not a graph
    node — so the lookup raises `KeyError` and the update is rejected. It is
    caught as non-fatal, which means repair has silently never worked on any
    thread whose last write came from summarization. In the 2026-08-13 incident
    it fired 5 times in 20 minutes and logged
    `Thread state repair check failed (non-fatal):
     'RailsSummarizationMiddleware.before_model'` every time; across 24 hours of
    mothership logs there is exactly one success.
    """

    class _Graph:
        def __init__(self, nodes):
            self.nodes = nodes

    class _App:
        """Rejects an as_node that isn't a real node, exactly as LangGraph does."""

        def __init__(self, messages, nodes=("__start__", "model", "tools", "__end__")):
            self._messages = messages
            self._nodes = nodes
            self.updated_with = None
            self.as_node = None

        def get_graph(self):
            return TestRepairSurvivesAMiddlewareLastWriter._Graph(self._nodes)

        async def aget_state(self, config):
            class _Snap:
                values = {"messages": self._messages}

            snap = _Snap()
            snap.values = {"messages": self._messages}
            return snap

        async def aupdate_state(self, config, update, as_node=None):
            resolved = as_node or "RailsSummarizationMiddleware.before_model"
            if resolved not in self._nodes:
                raise KeyError(resolved)
            self.updated_with = update
            self.as_node = resolved

    def _bricked_history(self):
        """An AIMessage with a tool_call nothing ever answered."""
        return [
            HumanMessage(content="build the thing", id="h1"),
            AIMessage(content="", id="a1", tool_calls=[
                {"name": "grep_files", "id": "call_1", "args": {"pattern": "x"}},
            ]),
        ]

    @pytest.mark.asyncio
    async def test_repair_is_accepted_when_summarization_wrote_last(self):
        from app.websocket.request_handler import RequestHandler

        app = self._App(self._bricked_history())
        await RequestHandler._repair_thread_state_if_needed(
            RequestHandler.__new__(RequestHandler),
            app,
            {"configurable": {"thread_id": "t1"}},
        )

        assert app.updated_with is not None, (
            "the repair was rejected — this thread stays bricked and the user's "
            "only escape is starting a new one"
        )
        assert app.as_node == "tools", (
            "answering dangling tool_calls should resume at the model, which is "
            "the edge out of the tools node"
        )

    @pytest.mark.asyncio
    async def test_it_falls_back_to_a_real_node_when_there_is_no_tools_node(self):
        from app.websocket.request_handler import RequestHandler

        app = self._App(
            self._bricked_history(), nodes=("__start__", "leonardo_beginner", "__end__"),
        )
        await RequestHandler._repair_thread_state_if_needed(
            RequestHandler.__new__(RequestHandler),
            app,
            {"configurable": {"thread_id": "t1"}},
        )

        assert app.as_node == "leonardo_beginner"

    def test_the_node_it_picks_exists_in_every_real_agent_shape(self):
        """`tools` must be a real node in both graph shapes, or this is a guess."""
        from app.websocket.request_handler import RequestHandler

        for nodes in (
            ("__start__", "model", "tools", "__end__"),          # create_agent
            ("__start__", "leonardo_beginner", "tools", "__end__"),  # raw StateGraph
        ):
            app = self._App([], nodes=nodes)
            assert RequestHandler._repair_as_node(app) in nodes

    def test_an_unreadable_graph_falls_back_to_the_old_default(self):
        """Never turn a best-effort repair into a new failure."""
        from app.websocket.request_handler import RequestHandler

        class _Broken:
            def get_graph(self):
                raise RuntimeError("no graph")

        assert RequestHandler._repair_as_node(_Broken()) is None
