# Oversized payloads and the summarization loop (SupportIncident #246)

**Status:** fixed in 0.6.0h · **Reported:** 2026-08-06 · **Box:** `leo-nefe` (paying retainer client)

A thread got stuck "in an infinite summarization loop where it keeps triggering
summarization and then can't even think before triggering it again". Compaction was
working correctly. It was being handed bytes it was structurally incapable of reclaiming.

This is **not** a regression of SI#106 (the delta-reducer / `REMOVE_ALL_MESSAGES` bug — see
`SUMMARIZATION_BUG.md`). That fix is present and intact.

## What was actually happening

Two payloads were injected fresh on every turn, neither with a size cap anywhere in the
stack.

**A. `debug_info.full_html` — the entire rendered page, every message.** The Rails page
answers `get_debug_info` with `document.documentElement.outerHTML`. On a page that inlines
every meeting transcript that document is ~10 MB. `request_handler` copied it into
LangGraph state verbatim ("pass everything else through naturally"), so it landed in every
checkpoint: **20 MB of checkpoint blobs for a three-message thread**, ~113 MB across five
threads in one afternoon. The agent never reads it — only `view_path` and `request_path`.
It also went into the docker logs on every message, because `_redact_frame` masked
credentials but never truncated.

**B. `<SELECTED_ELEMENT>` — unbounded `outerHTML` inside the message text.** The element
picker puts `target.outerHTML` straight into the message body. One picked `<section>` was
375 KB — ~95k tokens in a single `HumanMessage`. Unlike (A) this *does* reach the model and
*is* counted by the summarization trigger.

**Why compaction could never win.** `RailsSummarizationMiddleware` re-added the first 3
human messages verbatim with no byte budget, so a fat early message pinned the thread above
`SUMMARIZATION_TOKEN_THRESHOLD` permanently. And stock `SummarizationMiddleware` never
summarizes away the current turn's message, so an oversized newest message is always in the
preserved tail. Either way the post-compaction count stayed above the trigger, `before_model`
re-fired on the next step, and the thread could never recover — the customer had to abandon it.

The identical guard already existed for images only (`_strip_images_then_count` +
`SCREENSHOT_KEEP_RECENT`). There was no equivalent for HTML.

## The fix

Bound the class, not the two known fields.

| Layer | File | What it does |
|---|---|---|
| Ingestion (load-bearing) | `app/websocket/payload_limits.py` | Every non-routing frame field is size-bounded before it becomes state. `debug_info` leaves cap at 32 KB, any single state value at 128 KB, message text at 64 KB, a `<SELECTED_ELEMENT>` block at 24 KB. Truncated with `[truncated: N bytes omitted]`, never dropped, so `view_path`/`request_path` always survive. |
| Compaction budget | `app/agents/leonardo/summarization.py` | The preserved-initial messages now share a token budget (15% of the trigger). Anything under `ALWAYS_PRESERVE_TOKENS` (2000) is still kept verbatim; only budget-eating messages are truncated head-and-tail. |
| Loop breaker | `app/agents/leonardo/summarization.py` | After a compaction, if the count is *still* over the trigger, that is a guaranteed infinite loop. It logs at ERROR with before/after counts and the largest contributor, forces the payload under 80% of the trigger (see the ladder below), appends a note to the summary telling the agent to tell the user, and files a `report_friction` report. It also runs when summarization *declines* to compact (no safe cutoff), so an over-trigger payload is never simply handed to the provider. |
| Repair | `/compact` in `app/websocket/request_handler.py` | Truncates any single message over 15k tokens, so a thread that was *already* wedged can be rescued in place instead of started over. |
| Logging | `app/websocket/web_socket_handler.py` | `_redact_frame` truncates each logged value to 2 KB. |
| Defense in depth (Leonardo) | `rails/app/javascript/llamapress/payload_caps.js` | Caps `full_html` (64 KB) and picked-element `outerHTML` (24 KB) client-side, so the bytes don't cross the wire at all. |

Order matters: the **server-side cap is the load-bearing one**. It ships in the image and
reaches every box the moment it rolls, regardless of what a box's Rails overlay or stale
frontend is doing. `rails/app/javascript/llamapress` *is* on the `bin/update` ALLOWLIST so
the JS caps do propagate; the layout partial `_llamapress_page_context.html.erb` is **not**
allowlisted and is customer-editable, so its cap reaches new launches only.

### The escalation ladder (`_force_under_target`)

Truncating text is not always enough — tool-call arguments and image blocks aren't text, so
a message can be genuinely unshrinkable, and "we tried" still means an infinite loop for the
customer. So the enforcement escalates until the payload is under the target, and cannot
fail:

1. **Truncate** the largest text offenders (never the summary).
2. **Strip attachments** — images/media/file blocks are replaced by a placeholder. An image
   the agent can re-request beats a thread that can't take another turn.
3. **Drop whole messages**, oldest first, never the newest turn (that's what the user is
   waiting on) and never the summary. Tool-calling AIMessages travel with the ToolMessages
   that answer them — dropping half a pair doesn't loop, it 400s.
4. **Last resort:** hard-truncate whatever survives, summary included, and if even that isn't
   enough, hand the model **the summary alone with nothing attached**.

A thread that has lost its attachments still works. A thread that loops never does.

All caps are env-overridable (`DEBUG_INFO_VALUE_MAX_BYTES`, `STATE_VALUE_MAX_BYTES`,
`MESSAGE_TEXT_MAX_BYTES`, `SELECTED_ELEMENT_MAX_BYTES`) for a box that needs a bigger window
without a redeploy.

## Triage

The one-liner for a box that feels wedged — the whole frame is one physical log line, so use
`wc -L` (`awk` over SSH is unreliable here):

```bash
docker compose logs llamabot --since 60m 2>&1 | grep -a "Received message:" | wc -L
```

Tens of KB = healthy. `leo-nefe` returned **10,670,531**. After this fix the logged copy is
capped regardless, so also check the state itself if a thread misbehaves:

```bash
# an uncompactable thread now says so, loudly
docker compose logs llamabot 2>&1 | grep -a "did NOT get under the trigger"
```

Checkpoint bloat for a thread (`llamabot_production`):

```sql
SELECT thread_id, count(*) AS blobs, pg_size_pretty(sum(length(blob))::bigint) AS bytes
FROM checkpoint_blobs GROUP BY thread_id ORDER BY sum(length(blob)) DESC LIMIT 10;
```

## Tests

- `app/tests/test_payload_limits.py` — the ingestion caps, including the class-level guard:
  *no state value exceeds the cap whatever the key*, which catches the next unpredicted
  content-heavy field without anyone having to predict it.
- `app/tests/test_rails_summarization_middleware.py` (`TestSummarizationLoop`,
  `TestUncompactableThreadFailsLoud`, `TestThreadRepair`) — the loop itself, at the real
  production constants with the real token counter: post-compaction count under the trigger,
  and a second `before_model` returning `None`.
- `Leonardo/rails/spec/javascript/llamapress/payload_caps.test.js` — the client-side caps.

## Follow-ups not done here

- The frontend `TokenIndicator` could show an explicit "this conversation is too large to
  compact" state. With the loop breaker the thread self-heals, so the current surface is the
  agent telling the user; a dedicated indicator state is still nicer.
- Not storing `full_html` in state at all (an allowlist of `debug_info` keys) would remove
  the remaining 32 KB/turn. It was left as a cap rather than an allowlist because the legacy
  `view_page` tool in the llamapress html_agent reads the whole `debug_info` dict.
