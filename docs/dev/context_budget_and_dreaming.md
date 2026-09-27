# Context budget and "dreaming" (design, 0.7.11 — Phase 1, for review)

Status: **design only, target 0.7.12. Nothing here is built.** Hand-back for Darren/Kody per the
2026-09-23 ticket (`compaction-loop-oversized-project-memory-and-dreaming`).

## TL;DR

- **The loop is a harness bug, not a model bug.** We inject `.leonardo/LEONARDO.md` and
  `MEMORY.md` whole into every call. On leo-lozeki that was ~125k tokens of a 150k
  trigger, so compaction fired on 94.5% of calls and each pass threw away the file the
  agent had just read.
- **Fix it in the harness first (Phase 2a, safe, small):** a hard budget for injected
  project context with a pointer to the rest, a MEMORY.md that is an index rather than
  a dump, a trigger that counts only what compaction can reclaim, and a circuit breaker
  that refuses to compact when there is nothing to reclaim. This alone ends the loop.
- **Dreaming (Phase 2b, off by default):** an idle-time pass that rewrites the memory
  files into a small core plus topic files, archiving the originals. It improves what
  the agent remembers; it is not what stops the loop.

## 1. Best-practice survey

Checked against official docs on 2026-09-27. "Not documented" means the docs don't say.

| System | Always in context | On demand | Size limit on the always-loaded part | Compaction / consolidation |
|---|---|---|---|---|
| **Claude Code** | CLAUDE.md files and their imports; the first **200 lines / 25 KB** of the auto-memory `MEMORY.md` index | Memory topic files, nested CLAUDE.md, path-scoped rules | Index: 200 lines / 25 KB. It warns near the limit and returns an error on a write over it. CLAUDE.md should stay under 200 lines. | Auto-compact near the model limit. CLAUDE.md and memory are **re-injected, never summarized**. There is a **thrashing guard**: if context refills right after compaction several times, it stops with "Autocompact is thrashing". Dream pass: not in the official docs (only third-party write-ups). |
| **Codex CLI** | AGENTS.md chain | Memories (grep) | `project_doc_max_bytes` = **32 KiB** combined | `model_auto_compact_token_limit_scope = body_after_prefix` counts only growth after the fixed prefix, which is our §3.3. Memory consolidation runs in the background when idle and is skipped when rate-limited. |
| **Letta / MemGPT** | Core memory blocks (per-block character limit); MemFS `system/` plus the file tree | Archival memory and other MemFS folders | Per-block limit | **Sleep-time agents** consolidate memory every N steps or on compaction. Backups before restructuring; every edit is a git commit. |
| **LangChain** | Not applicable | Not applicable | Not applicable | `SummarizationMiddleware` triggers on tokens, fraction or messages. Whether it counts the system prompt is not documented. `ClearToolUsesEdit` replaces old tool outputs with a placeholder once over 100k, keeping the last 3. |
| **Anthropic API** | System prompt + tools (**"not summarized"**) | Memory tool (`/memories`, nothing preloaded, `view` truncates at 16k characters) | Not applicable | Server compaction (default 150k, minimum 50k). Context editing clears tool results, with `clear_at_least` to make sure clearing is worthwhile. |

Sources: code.claude.com/docs/en/memory, …/context-window, …/troubleshooting;
learn.chatgpt.com/docs/config-file/config-reference, …/customization/memories;
docs.letta.com/guides/agents/architectures/sleeptime, docs.letta.com/concepts/memfs;
docs.langchain.com/oss/python/langchain/middleware/built-in;
platform.claude.com/docs/en/build-with-claude/compaction, …/context-editing,
…/agents-and-tools/tool-use/memory-tool;
anthropic.com/engineering/effective-context-engineering-for-ai-agents.

**What this means for Leo:**
- Every system caps what is always loaded. Leo's ~125k tokens is 4–20× what any of them allows.
- The pattern everywhere is a small index plus files read on demand.
- Fixed context (the system prompt and memory) is kept out of the compaction count. Both Codex and Anthropic say so explicitly.
- A thrashing breaker is standard.
- Clearing old tool results is the cheapest form of compaction.
- Consolidation runs off the hot path and always keeps a backup.

## 2. What we do today (code as of 0.7.11)

| Piece | Where | Behaviour |
|---|---|---|
| LEONARDO.md / MEMORY.md injection | `project_context.py` `_load_md_file` → `build_system_prompt_with_project_context`, `build_beginner_system_prompt` | Whole file, no limit, every call. |
| When the prompt is built | `rails_agent/nodes.py:66` etc. | At graph compile (container start) for `create_agent` modes; per turn for the raw beginner/plan nodes. So a trim only reaches `rails_agent` after a restart. |
| MEMORY.md | `memory.py` `rebuild_memory_index()` | Writes every memory's **full content**, not an index. 50 × 2,000 chars ≈ 100 KB. Rewritten on every save, so a hand-trim does not stick. `MAX_MEMORIES = 50` is a hard stop: at the cap the agent cannot save anything. |
| Compaction trigger | `summarization.py` `_should_summarize` | Provider-reported context (system prompt + tools + messages) vs `SUMMARIZATION_TOKEN_THRESHOLD = 150_000`. Counts overhead compaction can never remove. |
| Kept tail | `SUMMARIZATION_KEEP_TOKENS = 30_000` | |
| User warning | `request_handler._warn_if_thread_is_wedged` | Fires once, at the **end** of a turn with ≥3 compactions (`COMPACTIONS_BEFORE_USER_WARNING`). A 3-hour turn shows nothing for 3 hours, and its advice ("start a new chat") cannot help here: the overhead comes back in every new thread. |
| Prompt guidance | `rails_beginner_agent/prompts.py:397` | "Always update LEONARDO.md when something meaningful changes." Nothing ever trims it. Mothership prompt overrides beat `prompts.py`, so any prompt change must also land there. |

**Did the wedged warning fire on leo-lozeki?** It should have fired at the end of nearly
every turn (435 compactions in one turn). It logs `Thread %s compacted %d times in one
turn` at ERROR. This box cannot see leo-lozeki's logs; the mothership can grep for that
line. Either way the warning is too late and gives the wrong advice for this cause.

## 3. Harness guards (Phase 2a — build first, on by default)

### 3.1 Hard budget for injected project context

- `PROJECT_CONTEXT_TOKEN_BUDGET = 12_000` tokens for LEONARDO.md + MEMORY.md combined
  (the fleet median first call is 29k; this keeps a bad box within ~12k of it).
  Counted with the same tiktoken counter the summarizer uses.
- **LEONARDO.md:** under ~8k tokens, inline it whole (no change for almost every box).
  Over that, inline the head up to the budget, **cut at a heading boundary**, then a
  generated outline of the remaining headings (first N, capped), then:
  > LEONARDO.md is 473 KB; only the start is shown above. Read the rest on demand with
  > `read_file .leonardo/LEONARDO.md` (use offsets). Do not append session logs to it.
  Same shape as `BRAND_INLINE_THRESHOLD` / the `brand-guidelines` skill pointer.
- **MEMORY.md:** see 3.2; injected as an index, capped at the remainder of the budget.
- Personality files (SOUL/USER/IDENTITY) get the same treatment with a small cap each;
  they have not been seen large, but nothing stops them.

### 3.2 MEMORY.md becomes an index

- `rebuild_memory_index()` writes one line per memory:
  `- [name](memory/<file>.md) — description` grouped by type, like Claude Code's
  auto-memory index. Content is read on demand with `read_file`.
- Raise `MAX_MEMORIES` (e.g. 200) now that the index, not the content, is in context;
  cap the **index** at the budget instead. At the cap, saving evicts nothing
  silently: it tells the agent to merge or delete, as today.
- One-time migration: first rebuild after upgrade rewrites the file; the old one goes
  to `.leonardo/archive/<date>/MEMORY.md` (no data loss; the per-memory files already
  hold the content).

### 3.3 Trigger on what compaction can reclaim

- Measure **fixed overhead** = system prompt + tool schemas, once per model call, in a
  `wrap_model_call` middleware that already sees `request.system_message` and
  `request.tools` (store it in a context var for the summarizer's next `before_model`,
  the same pattern as `_ACTIVE_CHAT_MODEL`).
- Reclaimable = reported context − fixed overhead. Compact when reclaimable exceeds
  `threshold − overhead`, i.e. the trigger is relative to the window **after** overhead.

### 3.4 Circuit breaker

- If `threshold − overhead < MIN_RECLAIMABLE` (proposal: 40k tokens — larger than the
  30k keep-tail plus one big file read), **do not compact.** Compacting there only
  destroys working memory.
- Log once per thread at WARNING with the numbers, record `breaker_tripped` on the turn,
  and send the user one in-chat notice per thread (through `turn_notices`, so it shows
  mid-turn, not at the end): "This project's notes are too large for me to work
  efficiently. I'll tidy them (or: ask me to tidy them)."
- With 3.1 in place the breaker should never trip; it is the backstop for whatever
  grows next (a huge tool schema set, a mothership prompt override).

### 3.5 Fix the warning

- Emit the wedged warning **mid-turn** at the 3rd compaction (via `turn_notices`), not
  only at the end.
- If the breaker or the overhead ratio says the cause is project context, say that
  instead of "start a new chat".

## 4. Dreaming (Phase 2b — behind a flag, off by default)

A background consolidation pass over `.leonardo/` that runs outside the user's turn.

**Trigger.** On the LeaseManager tick, when all of these hold:
- the box has been idle ≥ 30 min (no turn in flight; the lease loop already tracks
  activity),
- LEONARDO.md > 24 KB, **or** the memory index is over budget, **or** memories ≥ 90% of
  the cap, **or** the breaker tripped since the last dream,
- no dream in the last 24 h.
Also callable on demand: the agent can offer "tidy project notes" and the user can
ask for it.

**Flag.** `instance_overrides.dreaming: true` in the model policy (plus
`LEONARDO_DREAMING=true` for local testing). Off by default; per box until approved.

**Model and cost.** The box's platform default text model (DeepSeek Flash-class):
one call, input capped at ~150k (the files are chunked if larger), output ≤ 8k.
Roughly a cent per dream. It is platform spend, not a user message: it is not sent to
`report_message`, so it never counts against the customer's message cap or blocks them.
On a `customer_paid_only` or ZDR box it uses only the model those modes allow; with no
usable model it **skips the LLM step** and does the deterministic part only (archive +
head/outline core), which still fixes the budget.

**What it writes.**
- `.leonardo/archive/<YYYY-MM-DD>/` gets the untouched originals first. Never delete.
- `LEONARDO.md` → a core of ≤ 150 lines / ~6 KB: what the app is, current state,
  conventions, open work. Dedupe; drop session logs and "EMERGENCY FIX" diaries.
- Detail moves to `.leonardo/notes/<topic>.md`, listed at the bottom of the core with
  one line each (read on demand).
- Memories: merge duplicates, drop ones superseded by later ones, rebuild the index.
- A single git commit on the box, "Leo tidied project notes", so the change shows in
  history and one revert undoes it.

**How the user sees and undoes it.** The next chat turn gets a one-line system note:
"I tidied your project notes while you were away. The originals are in
`.leonardo/archive/<date>/`; say 'undo the tidy' to restore them." Undo restores the
archive (a tool, not a git command the user has to know).

**Prompt changes.** Replace "Always update LEONARDO.md" with "Update LEONARDO.md in
place and keep it short (under ~150 lines). Put detail in `.leonardo/notes/<topic>.md`
and link it. Never append session logs." Must land in the **mothership prompt
overrides** too, or the box keeps the old text.

**Pulling facts out of long threads.** Possible, and cheap to add later: when the
summarizer compacts, the same call can return "durable facts" that go to
`save_memory`. Not in the first cut — it needs its own review of what is worth
keeping, and the harness guards fix the incident without it.

## 5. Telemetry (add to the per-turn rollup)

| Field | Why |
|---|---|
| `project_context_tokens` | Injected LEONARDO/MEMORY tokens after the budget. |
| `project_context_truncated` | Whether the budget cut anything. |
| `fixed_overhead_tokens` | System prompt + tools, max over the turn's calls. |
| `reclaimable_at_trigger` | What compaction could actually free when it fired. |
| `compaction_breaker_tripped` | The breaker refused at least once this turn. |
| `leonardo_md_bytes`, `memory_md_bytes`, `memory_count` | Growth, visible without SSH. |
| `dream` (per dream, via `report_health`) | ran/skipped/reason, bytes before/after, model. |

With these, leo-lozeki's cause is one query: `fixed_overhead_tokens` near the trigger.

## 6. Phase 2 test plan (from the ticket, unchanged)

- A ~146k fixed prompt plus a 20k `read_file` result: no compaction; logs and warns.
- A 484 KB LEONARDO.md: injected context under the budget; the agent is told how to
  read the rest.
- Existing summarization, loop-guard and "trigger goes blind on Muse" tests still pass.
- Replay leo-lozeki's archived `.leonardo/` (Mother Leo's tarball, never the live box)
  with the CJ-01 prompt: compactions near 0, first-call input well under 50k, tens of
  steps.

## 7. Decisions for review

1. Budget of 12k tokens for injected project context — OK? (Claude Code's index cap is
   25 KB ≈ 6k tokens; Codex's is 32 KiB ≈ 8k. 12k is looser than both.)
1b. Also clear old tool results before summarizing (keep the last 3, like
   `ClearToolUsesEdit`)? It is cheaper than a summary, but it invalidates the prompt cache.
2. MEMORY.md as an index (breaking change to what the agent sees every turn) — OK?
3. Breaker threshold `MIN_RECLAIMABLE = 40k` — OK?
4. Dreaming on platform spend, off by default, per-box flag — OK?
5. Dreaming writes a git commit on the customer's repo — OK, or archive-only?
