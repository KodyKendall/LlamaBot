# Design: Background LangGraph runs + reconnect replay (Layer 2)

**Status:** Implemented (v1, in-memory). See "As-built" at the bottom.
**Author:** investigation for the 0.5.3a websocket bug
**Depends on / follows:** Layer 1 idempotency seatbelt (shipped — see below)

## Background: the two layers

A user reported (via a ChatGPT incident transcript) that a long build — "upload
spreadsheet → build an app" — kept restarting from scratch ("Let me start by
reading…"), with the same bootstrap prompt appearing repeatedly in the
transcript. Investigation traced this to a WebSocket reconnect re-firing the
same message with no server-side idempotency, against a stream that is
abandoned the moment the socket dies.

We split the fix into two layers:

- **Layer 1 — idempotency seatbelt (SHIPPED).** Stops the duplicate/restart
  loop. Small, in-memory, no lifecycle change. Details at the bottom of this
  doc.
- **Layer 2 — background runs + replay (THIS DOC).** Decouples the agent run
  from the live socket so a build keeps running when the browser drops, and a
  reconnecting client replays what it missed. This is the real fix for "let it
  keep running in the background."

## Problem (Layer 2)

Today the LangGraph `astream` consumer lives *inside* `RequestHandler.handle_request`,
which is the per-socket asyncio task. Two consequences:

1. **The run is bound to the socket.** `request_handler.py` breaks out of the
   `astream` loop the instant `_is_websocket_open(websocket)` is false
   (`request_handler.py` ~L526). The comment there is honest about why: with no
   observer, continuing would let the graph advance through tool nodes and write
   checkpoints that produce orphan ToolMessages. So the *abort-on-disconnect is a
   workaround for the absence of a background observer* — not a feature we want.

2. **A reconnect cannot resume an in-progress turn.** The LangGraph Postgres
   checkpointer persists thread *state* (keyed by `thread_id`), so history
   survives, but the interrupted assistant turn is neither finished nor
   re-streamed. The only recovery is `_repair_thread_state_if_needed` on the
   *next* user message — repair, not resume.

What we want:

> A run, once started, runs to completion regardless of the browser. The live
> socket is a *subscriber* to the run's output, not its lifeline. On (re)connect
> a client attaches to the running (or finished) run and replays anything it
> missed.

## Design

### 1. Decouple the run from the socket

Move the `astream` consumer out of `handle_request`'s socket-bound task into a
**background run task** keyed by `thread_id`. The task:

- drives the graph to completion (or to a HITL interrupt), regardless of socket
  state — so the `_is_websocket_open` *abort* at `request_handler.py` ~L526 is
  removed; the background task is now the observer that prevents orphan
  ToolMessages.
- writes every chunk it would have sent to the socket into a **per-thread output
  buffer** (below) instead of (or in addition to) the live socket.

A **run registry** maps `thread_id → RunHandle { task, last_seq, status }`.
Starting a turn:

- if no run is active for the thread → create one.
- if a run is already active for the thread → this is the duplicate/resend case;
  do **not** start a second run (Layer 1 already guards this by
  `client_message_id`; the registry is the structural backstop).

### 2. Per-thread output buffer with sequence numbers

Each emitted chunk gets a monotonically increasing `seq` (per thread). The
buffer is the "queue" the reconnecting client pulls from:

```
ThreadOutputLog(thread_id):
    append(chunk) -> seq          # assigns next seq, stores (seq, chunk)
    since(seq) -> list[(seq, chunk)]   # everything after the client's last seq
    last_seq -> int
```

Two viable backings:

- **In-memory ring buffer (start here).** Bounded (e.g. last N chunks or last
  few MB per thread). Lost on process restart — acceptable for v1 because the
  LangGraph checkpointer still holds the authoritative final state; a client
  that reconnects after a restart falls back to "reload thread from checkpoint."
- **Durable log (Redis stream / Postgres table).** Survives restarts and lets
  multiple processes serve the same thread. Heavier; do this only if we need
  cross-process or restart-durable replay. Redis Streams (`XADD`/`XRANGE`) map
  almost 1:1 onto `append`/`since`.

The live socket subscribes to the log: on each `append`, if a socket is attached
for that thread, push the chunk immediately.

### 3. Attach + replay on (re)connect

Replace the frontend resend-on-reconnect with **attach + replay**:

1. Client tracks the highest `seq` it has received for the active thread.
2. On (re)connect, after auth, client sends
   `{ type: "attach", thread_id, last_seq }`.
3. Server: `log.since(last_seq)` → send the missed chunks in order, then
   live-tail. If the run already finished, the client gets the tail and an
   `end`. If the thread isn't in the registry (e.g. after a restart), server
   tells the client to reload from the checkpointer.

This removes the need to re-submit the user message at all — which is exactly
what Layer 1's "don't resend an ACKed message" anticipates.

### 4. Lifecycle / cleanup

- A finished run stays in the registry + buffer for a short TTL so a slow
  reconnect can still replay, then is GC'd.
- Cancel semantics unchanged: an explicit `cancel` (or a genuinely new user
  message that supersedes the turn) cancels the background task.
- The existing `_repair_thread_state_if_needed` stays as a belt-and-suspenders
  for any state that still ends up dangling.

## Why this is tractable here

- **State is already durable.** `AsyncPostgresSaver` keyed by `thread_id`
  already persists conversation state across disconnects (`main.py` ~L180-215).
  Layer 2 adds *run liveness* + *output replay*, not state persistence.
- **It removes a hack rather than adding one.** The abort-on-disconnect exists
  *because* there is no background observer. Adding the observer lets us delete
  the abort.

## Risks / open questions

- **Backpressure:** a fast graph + slow/absent socket must not let the buffer
  grow unbounded → bound the ring buffer and drop-oldest with a replay-gap
  signal that tells the client to reload from checkpoint.
- **Single-writer assumption:** one background task per thread. If two browser
  tabs share a `thread_id`, both attach as subscribers to the same run — fine;
  but two *new turns* racing on one thread must serialize (the existing per-socket
  lock becomes a per-thread lock).
- **Process restart:** in-memory buffer is lost; the client falls back to
  checkpoint reload. Durable log removes this caveat at the cost of infra.
- **Mothership reporting** (`report_message`) already happens inside the stream
  loop; moving the loop to a background task keeps that intact and actually makes
  it more reliable (no longer aborted mid-stream).

## Phasing recommendation

1. Run registry + background task + in-memory ring buffer + attach/replay
   protocol; remove the `_is_websocket_open` abort.
2. Frontend: replace resend-on-reconnect with attach + `last_seq` replay.
3. (Optional, later) swap the in-memory buffer for a Redis stream for
   restart-durable / multi-process replay.

---

## Appendix: Layer 1 idempotency seatbelt (shipped)

The invariant: **a user message may be retried, but it may not be processed
twice.** Implemented as:

- `app/websocket/message_deduplicator.py` — `MessageDeduplicator`, an
  LRU-bounded registry of seen `(thread_id, client_message_id)` pairs, held on
  the app-level `WebSocketConnectionManager` so it survives reconnects (which
  create a fresh handler).
- `web_socket_handler.py` — before cancelling the in-flight task for a new chat
  message, it registers `(thread_id, client_message_id)`. A duplicate is ACKed
  with `status: "duplicate"` and **ignored** (the in-flight run is left intact);
  a new message is ACKed with `status: "accepted"` and processed. Clients that
  send no `client_message_id` (Rails gem, older browsers) are always treated as
  new and are unaffected.
- Frontend (`index.js`, `WebSocketManager.js`) — generates a
  `client_message_id` per message (stable across resends), tracks server ACKs,
  and only resends a message the server never ACKed. The backend guard is the
  real protection; the frontend gate avoids the needless round-trip.

Tests: `app/tests/test_message_deduplicator.py` and
`TestWebSocketIdempotency` in `app/tests/test_websocket.py`.

Layer 1 stops the duplicate/restart loop but does **not** resume a run whose
socket dropped mid-stream — that run is still abandoned until the next message.
Resuming it is the job of Layer 2 above.

---

## As-built (Layer 2 v1)

Shipped implementation, in-memory:

- `app/websocket/run_manager.py`
  - `ThreadOutputLog` — bounded, seq-numbered log per thread; `since(last_seq)`
    + `has_gap(last_seq)` for replay.
  - `RunSink` — quacks like a Starlette WebSocket (`send_json` + `client_state`
    always CONNECTED). This is the trick that kept the change small: RequestHandler's
    three streaming methods (`handle_request`, `handle_approval_response`,
    `handle_question_response`) pass the sink where they used to pass the socket,
    so every `await websocket.send_json(...)` / `_is_websocket_open(...)` site
    works unchanged — sends now log + best-effort-forward, and the run never
    aborts on a dead socket. The mid-stream abort in `handle_request` was removed.
  - `RunHandle` — a thread's run: task, log, and the currently-attached socket.
  - `RunManager` — app-level registry (`app.state.run_manager`, created in
    `main.py`). `start()` supersedes (cancels) any active run for the thread and
    reuses the log so `seq` stays monotonic across turns; `attach`/`detach`/
    `cancel`; LRU-bounds finished runs (never evicts a live one).
- `web_socket_handler.py`
  - Chat / approval / question messages start a background run via
    `RequestHandler.start_chat_run` / `start_resume_run` (owned by RunManager,
    not the connection).
  - New `attach` message → `_handle_attach`: replays `log.since(last_seq)` then
    live-tails; sends `no_active_run` / `replay_gap` when replay isn't possible.
  - `cancel` cancels the thread's run; disconnect (`finally`) DETACHES the
    socket from its threads — it does **not** cancel the runs.
- Frontend
  - `index.js`: on reconnect, if a run is in progress, sends
    `{type:"attach", thread_id, last_seq}` instead of re-sending the user
    message. `cancel`/stop carry `thread_id`. `websocketReplayUnavailable`
    (gap / no_active_run) stops the spinner.
  - `MessageHandler.js`: dedupes by per-thread `seq` (replay/live overlap is
    harmless), exposes `getLastSeq(threadId)`, and handles the `attached` /
    `no_active_run` / `replay_gap` control frames.
  - `WebSocketManager.js`: `attach` is non-queueable (connection-specific).

Tests: `app/tests/test_run_manager.py` (log/sink/manager: background-continues,
replay, supersede, cancel, detach, eviction) and `TestWebSocketLayer2Attach` in
`app/tests/test_websocket.py` (handler attach/replay/gap/no-run wiring).

### Known v1 limitations (follow-ups)

- **In-memory only.** A process restart loses the registry + logs; a reconnect
  then gets `no_active_run` and the client falls back to thread history. Durable
  replay (Redis stream / Postgres) is the phase-3 follow-up above.
- **No live re-attach after a full page reload.** Reconnect-attach only fires
  while the tab still believes a run is in progress (`isAgentRunning`). After a
  hard reload that flag is gone, so the still-running build completes invisibly
  and its result shows on the next thread load — it is not live-streamed. A
  durable "is this thread running?" probe on load would close this.
- **`replay_gap` / `no_active_run` just stop the spinner** rather than
  auto-reloading thread history. Good enough for v1; auto-reload is a polish item.
