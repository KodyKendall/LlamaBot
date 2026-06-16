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

from langchain_core.messages import AnyMessage
from typing_extensions import Required

from langgraph.channels.delta import DeltaChannel
from langgraph.graph.message import _messages_delta_reducer

# Write a full snapshot every N updates to the channel. Higher = less storage but
# more deltas to replay on resume; lower = bounded replay latency at the cost of
# more (small) snapshot blobs. 50 keeps reconstruction cheap even for long runs
# (recursion_limit is 450) while staying far below the per-checkpoint cost of the
# old full-serialization model. SummarizationMiddleware keeps the live message
# list small, so each snapshot blob is tiny.
DELTA_SNAPSHOT_FREQUENCY = 50

# Drop-in replacement for `messages: Annotated[list[AnyMessage], add_messages]`.
# Placing this on a subclass overrides the base `AgentState`/`MessagesState`
# default because subclass field annotations win during schema compilation.
DeltaMessages = Required[
    Annotated[
        list[AnyMessage],
        DeltaChannel(_messages_delta_reducer, snapshot_frequency=DELTA_SNAPSHOT_FREQUENCY),
    ]
]
