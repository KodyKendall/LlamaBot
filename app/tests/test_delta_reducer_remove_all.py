"""Regression tests for the DeltaChannel messages reducer honoring REMOVE_ALL.

Reproduces and guards the SummarizationMiddleware loop seen in production on
`mbc-preceptors` thread c77fce95 (SupportIncident #106): the stock
`_messages_delta_reducer` silently drops `RemoveMessage(REMOVE_ALL_MESSAGES)`,
so a summarize write `[RemoveMessage(ALL), summary, *preserved]` leaves the full
prior history in place. The post-summary token count never drops below
SUMMARIZATION_TOKEN_THRESHOLD, so summarization re-fires every turn.

These assert *structure* (reducer in/out), no LLM and no DB needed.
"""
from langchain_core.messages import HumanMessage, AIMessage, RemoveMessage
from langgraph.graph.message import _messages_delta_reducer, REMOVE_ALL_MESSAGES

from app.agents.utils.delta_state import messages_delta_reducer


def _base():
    return [
        HumanMessage(content="original user intent", id="h1"),
        AIMessage(content="assistant turn 1", id="a1"),
        HumanMessage(content="second user turn", id="h2"),
        AIMessage(content="assistant turn 2", id="a2"),
    ]


def _summary_write(preserved):
    summary = HumanMessage(content="Here is a summary of the conversation", id="sum1")
    return [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary, *preserved]


# --- the bug: stock reducer leaves history in place ---------------------------

def test_stock_reducer_drops_remove_all_marker():
    """Documents the upstream defect this module works around."""
    state = _base()
    result = _messages_delta_reducer(state, [_summary_write([state[3]])])
    # The summary is appended on top of all 4 originals instead of replacing them.
    assert len(result) == 5
    assert [m.id for m in result] == ["h1", "a1", "h2", "a2", "sum1"]


# --- the fix: our reducer clears history on REMOVE_ALL ------------------------

def test_remove_all_clears_prior_history():
    state = _base()
    result = messages_delta_reducer(state, [_summary_write([state[3]])])
    assert [m.id for m in result] == ["sum1", "a2"], (
        "after a summary write the reconstructed state must contain only the "
        "summary + preserved tail, not the pre-summary history"
    )


def test_post_summary_state_is_smaller_so_loop_cannot_recur():
    """The whole point: the post-summary list must shrink."""
    state = _base()
    result = messages_delta_reducer(state, [_summary_write([state[3]])])
    assert len(result) < len(state)


# --- normal (non REMOVE_ALL) behavior is unchanged ---------------------------

def test_passthrough_matches_upstream_without_remove_all():
    state = _base()
    write = [[AIMessage(content="new", id="n1")]]
    assert (
        [m.id for m in messages_delta_reducer(state, write)]
        == [m.id for m in _messages_delta_reducer(state, write)]
    )


def test_single_id_remove_still_tombstones():
    state = _base()
    result = messages_delta_reducer(state, [[RemoveMessage(id="h2")]])
    ids = [m.id for m in result]
    assert "h2" not in ids and ids == ["h1", "a1", "a2"]


def test_dedup_by_id_preserved_after_remove_all():
    # preserved tail keeps its original id; no duplicate should appear.
    state = _base()
    result = messages_delta_reducer(state, [_summary_write([state[2], state[3]])])
    ids = [m.id for m in result]
    assert ids == ["sum1", "h2", "a2"]
    assert len(ids) == len(set(ids))


# --- batching invariance: DeltaChannel replays deltas from snapshots ---------
# reducer(reducer(s, xs), ys) MUST equal reducer(s, xs + ys) or snapshot replay
# diverges from live state.

def _ids(msgs):
    return [m.id for m in msgs]


def test_batching_invariance_remove_all_in_second_batch():
    xs = [HumanMessage(content="x", id="x1")]
    ys = [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(content="y", id="y1")]
    combined = messages_delta_reducer(_base(), [xs + ys])
    stepwise = messages_delta_reducer(messages_delta_reducer(_base(), [xs]), [ys])
    assert _ids(combined) == _ids(stepwise) == ["y1"]


def test_batching_invariance_remove_all_in_first_batch():
    xs = [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(content="x", id="x1")]
    ys = [AIMessage(content="y", id="y1")]
    combined = messages_delta_reducer(_base(), [xs + ys])
    stepwise = messages_delta_reducer(messages_delta_reducer(_base(), [xs]), [ys])
    assert _ids(combined) == _ids(stepwise) == ["x1", "y1"]


def test_batching_invariance_no_remove_all():
    xs = [AIMessage(content="x", id="x1")]
    ys = [AIMessage(content="y", id="y1")]
    combined = messages_delta_reducer(_base(), [xs + ys])
    stepwise = messages_delta_reducer(messages_delta_reducer(_base(), [xs]), [ys])
    assert _ids(combined) == _ids(stepwise)


def test_last_remove_all_wins_when_multiple():
    state = _base()
    write = [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        HumanMessage(content="discarded", id="d1"),
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        HumanMessage(content="kept", id="k1"),
    ]
    result = messages_delta_reducer(state, [write])
    assert _ids(result) == ["k1"]
