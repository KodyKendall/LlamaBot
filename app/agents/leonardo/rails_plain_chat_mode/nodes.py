"""Plain chat mode: an LLM and nothing else.

Deliberately the simplest graph in the repo — one node, no tools, no ToolNode, no
conditional edges. It cannot read, write, or run anything; it can only talk. That
is the whole point: it's the mode granted to the `user` role (see
app/permissions.py DEFAULT_ROLE_MODES), so its capability ceiling IS the
permission boundary. Adding a tool here silently widens what that role can do.

Reuses RailsAgentState so the fields the frontend already sends (llm_model,
debug_info, ...) pass through unchanged, and so thread state stays compatible if
a user switches modes mid-thread.
"""
import logging

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from app.agents.leonardo.llm_factory import get_llm, system_message_for_model
# The box's resolved default (Muse where the box has a META key, DeepSeek
# where it does not) — never a hardcoded id, or a turn that arrives without
# an explicit llm_model silently ignores the fleet default.
from app.agents.leonardo.model_policy import enabled_default_model
from app.agents.leonardo.rails_agent.state import RailsAgentState
from app.agents.leonardo.project_context import brand_context_section
from app.agents.leonardo.resilience import invoke_with_transient_retry

load_dotenv()

logger = logging.getLogger(__name__)

PLAIN_CHAT_PROMPT = """You are a helpful assistant inside LlamaPress.

You are in plain chat mode: you can talk with the user, answer questions, explain
things, and help them think. You have no tools — you cannot read or edit files,
run commands, browse the web, or change their app.

If the user asks you to DO something to their app, say plainly that you can't in
this mode, and tell them to switch modes using the mode selector to get an agent
that can. Don't guess at file contents or pretend to have made a change."""


def get_sys_msg():
    # Rebuilt per turn so a brand-guide edit lands without a restart (same
    # reasoning as the other raw-graph agents' get_sys_msg).
    return {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": PLAIN_CHAT_PROMPT + brand_context_section(),
                "cache_control": {"type": "ephemeral"},
            },
        ],
    }


def plain_chat(state: RailsAgentState):
    llm_model = state.get("llm_model") or enabled_default_model()
    logger.info(f"Using LLM model: {llm_model}")

    # No .bind_tools() — see the module docstring before adding any.
    llm = get_llm(llm_model)

    sys_msg = system_message_for_model(get_sys_msg(), llm_model)
    # Raw node — no DynamicModelMiddleware, so the rung-1 transient retry has to
    # be at the call site, same as rails_beginner_agent / rails_ai_builder_agent.
    # Without it a mid-stream provider drop (RemoteProtocolError: incomplete
    # chunked read) kills the turn outright. See test_stream_truncated_response.py.
    response = invoke_with_transient_retry(
        lambda: llm.invoke([sys_msg] + state["messages"]),
        label=f"rails_plain_chat_mode/{llm_model}",
    )
    return {"messages": [response]}


def build_workflow(checkpointer=None):
    builder = StateGraph(RailsAgentState)
    builder.add_node("plain_chat", plain_chat)
    builder.add_edge(START, "plain_chat")
    builder.add_edge("plain_chat", END)
    return builder.compile(checkpointer=checkpointer)
