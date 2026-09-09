# Model-independent context compaction (0.7.7)

**Symptom:** Muse Spark threads reached 600k+ tokens while the 150k compaction trigger
never fired. Switching models made it worse, not better.

**Cause:** compaction was tied to a specific model in three places.

1. **The trigger never saw the real count.** It was a local tiktoken (OpenAI tokenizer)
   estimate of the checkpoint state. LangChain's `SummarizationMiddleware` *can* trust the
   provider's reported usage, but only when the summarizer's provider matches the chat
   model's — and ours never matched, because the summarizer was picked by which API key
   exists on the box (DeepSeek first), not by the model in use.
2. **Custom-endpoint clients streamed no usage at all.** `ChatOpenAI` leaves
   `stream_usage` unset for a custom `base_url` (Muse, GMI, Fireworks, RunPod,
   OpenRouter), so a streamed turn carried no `usage_metadata` to read.
3. **Sub-agents ran with no middleware.** `delegate_task` / `delegate_research` built a
   bare `create_agent`: no compaction, no tool-output cap.

Also: when the key-chain summarizer failed (no key for it on that box), LangChain stored
the literal `Error generating summary: ...` as the thread's summary.

## What changed

All in `app/agents/leonardo/summarization.py` unless noted.

- `reported_context_tokens(messages, counter)` — the newest AI message's
  `usage_metadata` (input + output) plus the estimate of everything after it. The
  trigger uses the larger of this and the estimate. No provider check: the number is
  what got billed.
- **Calibration.** `calibration_ratio()` = reported ÷ estimated at the message that
  reported, capped at `MAX_CALIBRATION_RATIO` (8). Every count the middleware makes —
  trigger, keep-tail cutoff, loop guards, summary trim — is the estimate times this
  ratio, so a model whose real tokens run 3x tiktoken keeps a 30k *real* tail and lands
  under the real trigger. Without it the reported number fired the trigger but the
  cutoff search, still counting in estimate tokens, kept everything.
- A compaction **clears `usage_metadata`** from the AI messages it keeps. They were
  produced against the old context; left in place they would re-fire the trigger on the
  next call (the summarize-every-turn loop by another door).
- The summarizer is **the model in use** (`state["llm_model"]` via `get_llm`), then the
  key chain (`make_summarization_model`) as fallback. If every summarizer fails the
  stored summary is `SUMMARY_UNAVAILABLE_TEXT` — an honest notice, never an exception.
- `llm_factory._apply_stream_usage` sets `stream_usage=True` on every client that has
  the field, post-construction (same pattern as `_apply_stream_chunk_timeout`, same
  reason: eleven call sites). Kill switch: `LLM_STREAM_USAGE=false`.
- `rails_agent/sub_agents.py` — both sub-agents get `make_summarization_middleware` +
  `ToolResultSizeLimitMiddleware`.
- Raw StateGraph nodes pass `llm_model=` to `compact_messages_if_needed`.

## Tests

- `tests/test_compaction_reported_usage.py` — trigger on reported usage, stale-usage
  clearing, no re-trigger.
- `tests/test_summarizer_uses_chat_model.py` — chat model first, key chain fallback,
  no error string stored.
- `tests/test_stream_usage_enabled.py` — every OpenAI-compatible client streams usage.
- `tests/test_sub_agents_context_middleware.py` — sub-agents carry the stack.

## Things to know

- **A provider that rejects `stream_options`** shows up as a 400 on every streamed turn
  (not a stall). Set `LLM_STREAM_USAGE=false` on that box and file the provider.
- **Residual: contexts the estimate cannot see.** Media blocks are estimated at a flat
  1000 tokens. A thread that is huge only because of one video/PDF attachment has a
  calibration ratio far above the cap, so compaction stays gentle and the attachment
  message itself may survive in the tail. That needs real media token accounting; it is
  not the long-conversation case this fixes.
- `/api/thread-tokens` (the chat's context meter) still shows the estimate.
