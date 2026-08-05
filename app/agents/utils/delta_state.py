"""Shared DeltaChannel-backed `messages` field for agent state schemas.

Background
----------
By default LangGraph's `AgentState.messages` uses the `add_messages` reducer,
which re-serializes the *entire* accumulated message list into every checkpoint.
For long agent threads that is O(N^2) storage growth and was the reason the
checkpoint tables ballooned on disk (see `app/services/checkpoint_cleanup.py`,
which used to aggressively trim intermediate checkpoints to compensate).

`DeltaChannel` (LangGraph 1.2 beta) stores only the *incremental delta* written
at each step, plus a full snapshot every `snapshot_frequency` updates. Storage
grows ~O(N) instead of O(N^2) — LangChain's own benchmark reports ~41x less
checkpoint storage for a long-running agent. Reconstruction walks back from the
latest checkpoint to the nearest snapshot, replaying the deltas stored in
`checkpoint_writes`.

IMPORTANT — interaction with cleanup
------------------------------------
Because reconstruction replays ancestor writes back to the last snapshot,
deleting intermediate `checkpoint_writes`/`checkpoint_blobs` for a thread (as the
old `cleanup_thread_checkpoints_except_latest` did) would destroy the delta chain
and make the latest checkpoint unreconstructable. Any agent using `DeltaMessages`
must NOT be subjected to that per-thread partial cleanup. See
`app/services/checkpoint_cleanup.py`.

`DeltaChannel` is beta; its on-disk representation may change between LangGraph
releases. Keep the version pins in `requirements.txt` in lockstep.
"""
from typing import Annotated

from langchain_core.messages import AnyMessage, RemoveMessage
from typing_extensions import Required

from langgraph.channels.delta import DeltaChannel
from langgraph.graph.message import _messages_delta_reducer, REMOVE_ALL_MESSAGES


def messages_delta_reducer(state, writes):
    """`_messages_delta_reducer` that also honors `RemoveMessage(REMOVE_ALL_MESSAGES)`.

    Why this exists
    ---------------
    The upstream `_messages_delta_reducer` (LangGraph 1.2) explicitly does NOT
    implement `REMOVE_ALL_MESSAGES` semantics — its docstring says so. When it
    sees `RemoveMessage(id="__remove_all__")` it looks the id up in its index,
    finds nothing (no real message has that id), and silently drops the marker.

    `SummarizationMiddleware` (and our `ToolResultImageClearingMiddleware`) clear
    history by writing::

        [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary, *preserved]

    With the stock reducer the marker is dropped and `summary`/`preserved` are
    appended on top of the *full* prior history (preserved ones updated in place
    by id). The post-summary message list therefore never shrinks, its token
    count stays above `SUMMARIZATION_TOKEN_THRESHOLD`, and summarization
    re-triggers on every single turn — the production loop seen on
    `mbc-preceptors` thread c77fce95 (SupportIncident #106), where the checkpoint
    history raced from step ~193 to ~435 with repeated `loop` checkpoints.

    Semantics
    ---------
    A `RemoveMessage(REMOVE_ALL_MESSAGES)` discards EVERYTHING accumulated before
    it — both the prior `state` and any earlier messages in the same write batch —
    keeping only the messages that follow the LAST such marker. The remainder is
    then handed to the upstream reducer so its dedup-by-id / tombstoning / message
    coercion behavior is preserved unchanged.

    Batching invariance
    -------------------
    `DeltaChannel` requires the reducer to satisfy
    ``reducer(reducer(s, xs), ys) == reducer(s, xs + ys)`` so it can replay deltas
    from a snapshot. Treating REMOVE_ALL as "reset everything accumulated so far,
    continue with what follows" preserves that property (verified for the marker
    appearing in either batch, both, or neither). Without this invariant,
    DeltaChannel reconstruction from snapshots would diverge.
    """
    # Flatten writes exactly like the upstream reducer does.
    flat = []
    for w in writes:
        if isinstance(w, list):
            flat.extend(w)
        else:
            flat.append(w)

    # Find the LAST REMOVE_ALL marker. Everything up to and including it — the
    # prior state plus earlier messages in this batch — is discarded.
    last_remove_all = -1
    for i, m in enumerate(flat):
        if isinstance(m, RemoveMessage) and getattr(m, "id", None) == REMOVE_ALL_MESSAGES:
            last_remove_all = i

    if last_remove_all >= 0:
        return _messages_delta_reducer([], [flat[last_remove_all + 1:]])
    return _messages_delta_reducer(state, [flat])

# Write a full snapshot every N updates to the channel. Higher = less storage but
# more deltas to replay on resume; lower = bounded replay latency at the cost of
# more (small) snapshot blobs. 50 keeps reconstruction cheap even for long runs
# (recursion_limit is 900) while staying far below the per-checkpoint cost of the
# old full-serialization model. SummarizationMiddleware keeps the live message
# list small, so each snapshot blob is tiny.
DELTA_SNAPSHOT_FREQUENCY = 50

# Drop-in replacement for `messages: Annotated[list[AnyMessage], add_messages]`.
# Placing this on a subclass overrides the base `AgentState`/`MessagesState`
# default because subclass field annotations win during schema compilation.
DeltaMessages = Required[
    Annotated[
        list[AnyMessage],
        DeltaChannel(messages_delta_reducer, snapshot_frequency=DELTA_SNAPSHOT_FREQUENCY),
    ]
]
