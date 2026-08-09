"""Shared summarization middleware for the Leonardo agent fleet.

`make_summarization_middleware()` is the single source of truth for how every
agent compacts long conversations. It wires together:

- the provider fallback model (`make_summarization_model`: DeepSeek -> Gemini ->
  OpenAI -> Anthropic),
- a token-budgeted retention policy (`keep=("tokens", SUMMARIZATION_KEEP_TOKENS)`)
  so the preserved recent tail is bounded by tokens and auto-trimmed by the
  middleware's binary-search cutoff rather than a fixed message count,
- a screenshot-stripping token counter so accumulated browser_inspect images
  never inflate the count,
- and `RailsSummarizationMiddleware`, which additionally preserves the first few
  user messages verbatim and re-injects the most recent todo list.

Why a subclass
--------------
Stock `SummarizationMiddleware` keeps only the recent suffix; the original user
intent and the live todo list disappear into the summary text. After a long run
that loses the thread's north star. `RailsSummarizationMiddleware` post-processes
the stock output to:

1. Re-add the first K human messages verbatim (the original ask), and
2. Append the last `write_todos` payload to the summary with an instruction to
   immediately restore it — keeping the todo list active across compaction.

The stock middleware emits `[RemoveMessage(REMOVE_ALL_MESSAGES), summary, *tail]`.
That `REMOVE_ALL_MESSAGES` only actually clears history because of the custom
DeltaChannel reducer in `app/agents/utils/delta_state.py`; without it the whole
thing loops (SupportIncident #106).
"""
from __future__ import annotations

import json
import logging

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import HumanMessage

from app.lib.text_budget import shrink_to_token_budget

logger = logging.getLogger(__name__)

# --- Budgets that keep compaction winnable (SupportIncident #246) -------------
#
# Re-adding the first K human messages verbatim with NO byte budget is what made
# a wedged thread unrecoverable: one 375 KB `<SELECTED_ELEMENT>` message pinned
# the count above the trigger permanently, so `before_model` re-summarized on
# every step and the agent never got a turn's work done. Preserving the user's
# original *intent* — the documented point of the feature — does not require
# preserving the markup they happened to have picked.

# Total token budget for all preserved-initial messages, as a fraction of the
# summarization trigger.
INITIAL_PRESERVE_BUDGET_RATIO = 0.15

# A message this small is always preserved verbatim, budget or not. Ordinary
# chat messages must never be truncated by a rule aimed at pasted markup.
ALWAYS_PRESERVE_TOKENS = 2000

# Below this, there is no point truncating a message down to a stub.
MIN_PRESERVE_TOKENS = 400

# After a compaction the total must land here (fraction of the trigger), leaving
# headroom for the next turn. Landing at 0.99 of the trigger is still a loop.
POST_COMPACTION_TARGET_RATIO = 0.8

_TRUNCATION_NOTICE = (
    "\n\n## NOTE: OVERSIZED CONTENT WAS TRUNCATED\n"
    "This conversation contains content too large to keep in context (usually a very "
    "large page element pasted in by the element picker, or a huge page). It was "
    "truncated so this thread can keep working. If you need that content, re-read the "
    "relevant file or page directly instead of relying on the conversation history, and "
    "tell the user that some earlier content was dropped — if the thread keeps "
    "struggling, suggest they start a new one."
)


def _strip_images_then_count(counter):
    """Wrap a token counter so old browser_inspect screenshots are stripped first.

    Each screenshot is ~25-50k tokens; counting them keeps the conversation above
    the summarization threshold on every turn. `ToolResultImageClearingMiddleware`
    strips them from state, and counting the stripped view here keeps the trigger
    check honest in the same turn that stripping happens.
    """
    from app.agents.utils.token_counter import _strip_old_images

    def _counter(messages):
        return counter(_strip_old_images(list(messages)))

    return _counter


def fit_message(msg, max_tokens, count_text):
    """A copy of `msg` truncated to ~`max_tokens`, keeping its id and role.

    Only text is shrunk. Tool-call args and media blocks are left alone so a size
    fix can never break an AI/Tool pair or corrupt an attachment — a wedged
    thread is bad, a thread with a dangling tool_call_id doesn't run at all.
    """
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        fitted = shrink_to_token_budget(content, max_tokens, count_text)
        if fitted is content:
            return msg
        return msg.model_copy(update={"content": fitted})

    if isinstance(content, list):
        text_blocks = [
            b for b in content
            if isinstance(b, dict) and b.get("type") in ("text", "text_delta")
        ]
        if not text_blocks:
            return msg
        per_block = max(MIN_PRESERVE_TOKENS, max_tokens // len(text_blocks))
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "text_delta"):
                fitted = dict(block)
                fitted["text"] = shrink_to_token_budget(
                    block.get("text", "") or "", per_block, count_text,
                )
                new_content.append(fitted)
            else:
                new_content.append(block)
        return msg.model_copy(update={"content": new_content})

    return msg


def truncate_oversized_messages(messages, max_tokens, token_counter):
    """Shrink any single message above `max_tokens`. Returns (messages, count).

    The repair half of the loop fix: `/compact` calls this so a thread that was
    already wedged before this shipped can be rescued in place, instead of the
    customer losing their context and starting over.
    """
    def _count_text(text):
        return token_counter([HumanMessage(content=text)])

    out = []
    truncated = 0
    for msg in messages:
        before = token_counter([msg])
        if before <= max_tokens:
            out.append(msg)
            continue
        fitted = fit_message(msg, max_tokens, _count_text)
        after = token_counter([fitted])
        if after < before:
            truncated += 1
            logger.warning(
                "Truncated oversized message %s during repair: %d -> %d tokens",
                getattr(msg, "id", None), before, after,
            )
            out.append(fitted)
        else:
            out.append(msg)
    return out, truncated


class RailsSummarizationMiddleware(SummarizationMiddleware):
    """`SummarizationMiddleware` that preserves original intent and the todo list.

    On top of the stock summarize-and-keep-recent behavior it:
      - re-adds the first ``keep_initial_human`` user messages verbatim, so the
        agent never loses the original request, and
      - appends the most recent ``write_todos`` payload to the summary with an
        instruction to restore it immediately, so todos survive compaction.
    """

    def __init__(self, *args, keep_initial_human: int = 3, **kwargs):
        super().__init__(*args, **kwargs)
        self.keep_initial_human = keep_initial_human

    # -- hooks ----------------------------------------------------------------

    def before_model(self, state, runtime):
        original = list(state["messages"])
        result = super().before_model(state, runtime)
        if result is None:
            return self._enforce_without_summary(original, runtime)
        return self._augment(original, result, runtime)

    async def abefore_model(self, state, runtime):
        original = list(state["messages"])
        result = await super().abefore_model(state, runtime)
        if result is None:
            return self._enforce_without_summary(original, runtime)
        return self._augment(original, result, runtime)

    # -- budgets --------------------------------------------------------------

    def _trigger_token_threshold(self):
        """The token count that fires summarization, or None if not token-based."""
        for kind, value in getattr(self, "_trigger_conditions", None) or []:
            if kind == "tokens":
                return int(value)
        return None

    def initial_preserve_budget(self):
        """Total tokens the preserved-initial messages may occupy."""
        threshold = self._trigger_token_threshold()
        if threshold is None:
            return None
        return max(ALWAYS_PRESERVE_TOKENS, int(threshold * INITIAL_PRESERVE_BUDGET_RATIO))

    def _count(self, messages):
        try:
            return self.token_counter(list(messages))
        except Exception:  # a counter must never be the thing that breaks a turn
            return 0

    def _count_text(self, text):
        return self._count([HumanMessage(content=text)])

    def _fit_message(self, msg, max_tokens):
        return fit_message(msg, max_tokens, self._count_text)

    # -- augmentation ---------------------------------------------------------

    @staticmethod
    def _is_summary(msg):
        return (getattr(msg, "additional_kwargs", None) or {}).get("lc_source") == "summarization"

    def _augment(self, original_messages, result, runtime=None):
        """Rebuild the summarize write to also keep first-K humans + the todo list."""
        msgs = list(result.get("messages", []))
        if not msgs:
            return result

        # Stock shape: [RemoveMessage(REMOVE_ALL_MESSAGES), summary, *preserved]
        remove_op = msgs[0]
        body = msgs[1:]

        summary_idx = next(
            (
                i
                for i, m in enumerate(body)
                if isinstance(m, HumanMessage)
                and (getattr(m, "additional_kwargs", None) or {}).get("lc_source")
                == "summarization"
            ),
            None,
        )
        if summary_idx is None:
            # Unexpected shape (upstream changed); leave the stock result alone.
            return result

        summary_msg = body[summary_idx]
        preserved = body[summary_idx + 1:]
        preserved_ids = {getattr(m, "id", None) for m in preserved}

        # (1) First-K human messages verbatim (skip any already in the tail).
        initial = self._first_human_messages(original_messages, preserved_ids)

        # (2) Restore the live todo list via the summary.
        todos = self._extract_last_todos(original_messages)
        if todos:
            summary_msg = self._with_todo_restore(summary_msg, todos)
            logger.info(
                "RailsSummarizationMiddleware: re-injected %d todo(s) into summary",
                len(todos),
            )

        if initial:
            logger.info(
                "RailsSummarizationMiddleware: preserved %d initial user message(s)",
                len(initial),
            )

        compacted = self._break_loop_if_still_oversized(
            [*initial, summary_msg, *preserved], runtime,
        )
        return {"messages": [remove_op, *compacted]}

    def _first_human_messages(self, messages, exclude_ids):
        """First `keep_initial_human` real user messages, within a token budget.

        Verbatim used to mean *verbatim*, with no ceiling — so a single fat early
        message (a picked page element, a pasted file) was re-added on every
        compaction and pinned the thread above the trigger forever. Short
        messages are still preserved untouched; only the ones that would eat the
        budget are truncated, head-and-tail, with an explicit marker.
        """
        budget = self.initial_preserve_budget()
        out = []
        spent = 0
        for m in messages:
            if len(out) >= self.keep_initial_human:
                break
            if not isinstance(m, HumanMessage):
                continue
            if (getattr(m, "additional_kwargs", None) or {}).get("lc_source") == "summarization":
                continue
            if getattr(m, "id", None) in exclude_ids:
                continue

            if budget is None:
                out.append(m)
                continue

            cost = self._count([m])
            if cost <= ALWAYS_PRESERVE_TOKENS or spent + cost <= budget:
                out.append(m)
                spent += cost
                continue

            allowance = budget - spent
            if allowance < MIN_PRESERVE_TOKENS:
                logger.warning(
                    "RailsSummarizationMiddleware: dropping initial message %s from the "
                    "preserved set (%d tokens, budget %d exhausted)",
                    getattr(m, "id", None), cost, budget,
                )
                continue

            fitted = self._fit_message(m, allowance)
            fitted_cost = self._count([fitted])
            if fitted_cost > allowance * 1.5:
                # Nothing shrinkable (media / tool args); keeping it would defeat
                # the whole point of the budget.
                logger.warning(
                    "RailsSummarizationMiddleware: dropping unshrinkable initial message "
                    "%s from the preserved set (%d tokens)",
                    getattr(m, "id", None), cost,
                )
                continue

            logger.warning(
                "RailsSummarizationMiddleware: truncated preserved initial message %s "
                "from %d to %d tokens to stay within the %d-token preserve budget",
                getattr(m, "id", None), cost, fitted_cost, budget,
            )
            out.append(fitted)
            spent += fitted_cost
        return out

    # -- loop breaker ---------------------------------------------------------

    def _break_loop_if_still_oversized(self, messages, runtime=None):
        """Guarantee the compacted thread is actually under the trigger.

        If a compaction lands back above the trigger, `before_model` fires again
        on the very next step and the agent spends every turn summarizing instead
        of thinking — the thread is dead and the user only sees a spinner. That
        happens whenever a single message is bigger than the whole budget (stock
        `SummarizationMiddleware` never summarizes away the current turn's
        message, so it is always in the preserved tail).

        So: fail loud, and force the count under the target instead of handing
        back a state we know will loop.
        """
        threshold = self._trigger_token_threshold()
        if threshold is None:
            return messages

        counts = [self._count([m]) for m in messages]
        total = sum(counts)
        if total < threshold:
            return messages

        target = int(threshold * POST_COMPACTION_TARGET_RATIO)
        biggest = max(range(len(messages)), key=lambda i: counts[i])
        logger.error(
            "RailsSummarizationMiddleware: summarization did NOT get under the trigger "
            "(%d tokens after compaction vs trigger %d) — this thread would re-summarize "
            "on every step forever. Force-truncating to %d tokens. Largest contributor: "
            "%s id=%s (%d tokens).",
            total, threshold, target, type(messages[biggest]).__name__,
            getattr(messages[biggest], "id", None), counts[biggest],
        )

        out = self._force_under_target(list(messages), counts, target)

        # Tell the agent what happened, so it can tell the user instead of
        # silently answering from a conversation with holes in it.
        out = [
            self._with_appended_note(m, _TRUNCATION_NOTICE) if self._is_summary(m) else m
            for m in out
        ]
        self._report_compaction_friction(total, threshold, messages[biggest], runtime)
        return out

    def _force_under_target(self, messages, counts, target):
        """Get `messages` under `target` tokens, escalating until it is.

        The point is that this cannot fail. Truncating text is not always enough —
        tool-call arguments and image blocks aren't text, so a message can be
        unshrinkable — and "we tried" still means an infinite summarization loop
        for the customer. So it escalates: truncate, then strip attachments, then
        drop whole messages oldest-first, and in the last resort hands the model a
        single summary message with nothing else attached to it. A thread that has
        lost its attachments still works. A thread that loops never does.
        """
        # (1) Truncate the biggest text offenders.
        for _ in range(20):
            if sum(counts) <= target:
                return messages
            idx = max(
                range(len(messages)),
                key=lambda i: -1 if self._is_summary(messages[i]) else counts[i],
            )
            if counts[idx] <= MIN_PRESERVE_TOKENS:
                break
            allowance = max(MIN_PRESERVE_TOKENS, target - (sum(counts) - counts[idx]))
            fitted = self._fit_message(messages[idx], allowance)
            fitted_cost = self._count([fitted])
            if fitted_cost >= counts[idx]:
                break  # unshrinkable by truncation — escalate
            logger.warning(
                "RailsSummarizationMiddleware: force-truncated message %s from %d to %d tokens",
                getattr(messages[idx], "id", None), counts[idx], fitted_cost,
            )
            messages[idx] = fitted
            counts[idx] = fitted_cost

        # (2) Strip attachments. An image the agent can re-request beats a thread
        # that can't take another turn.
        stripped = [self._strip_media(m) for m in messages]
        if any(a is not b for a, b in zip(stripped, messages)):
            messages = stripped
            counts = [self._count([m]) for m in messages]
            logger.warning(
                "RailsSummarizationMiddleware: stripped attachments from the compacted "
                "conversation to fit the context window (now %d tokens)", sum(counts),
            )
        if sum(counts) <= target:
            return messages

        # (3) Drop whole messages, oldest first. Tool-call groups go together so a
        # size fix can never leave a dangling tool_call_id (which doesn't just
        # loop — it 400s).
        groups = self._pairing_groups(messages)
        dropped = set()
        for gi, group in enumerate(groups):
            if sum(counts) <= target:
                break
            if gi == len(groups) - 1:
                break  # the newest turn is what the user is waiting on
            if all(self._is_summary(messages[i]) for i in group):
                continue
            for idx in group:
                logger.warning(
                    "RailsSummarizationMiddleware: dropped message %s (%d tokens) — the "
                    "conversation could not be compacted any other way",
                    getattr(messages[idx], "id", None), counts[idx],
                )
                counts[idx] = 0
                dropped.add(idx)
        if dropped:
            messages = [m for i, m in enumerate(messages) if i not in dropped]
            counts = [c for i, c in enumerate(counts) if i not in dropped]
        if sum(counts) <= target:
            return messages

        # (4) Last resort: hard-truncate whatever survives, summary included.
        for idx, msg in enumerate(messages):
            if sum(counts) <= target:
                break
            allowance = max(MIN_PRESERVE_TOKENS, target - (sum(counts) - counts[idx]))
            fitted = self._fit_message(msg, allowance)
            fitted_cost = self._count([fitted])
            if fitted_cost < counts[idx]:
                messages[idx] = fitted
                counts[idx] = fitted_cost
        if sum(counts) > target:
            logger.error(
                "RailsSummarizationMiddleware: conversation is STILL %d tokens after "
                "dropping and truncating everything droppable (target %d). Keeping the "
                "summary only.", sum(counts), target,
            )
            summary_only = [m for m in messages if self._is_summary(m)]
            if summary_only:
                return summary_only
        return messages

    @staticmethod
    def _strip_media(msg):
        """A copy of `msg` with image/media/file blocks replaced by a placeholder."""
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            return msg
        kept = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") in ("image_url", "media", "file"))
        ]
        if len(kept) == len(content):
            return msg
        kept.append({
            "type": "text",
            "text": "[Attachment removed to fit the context window. Ask for it again if you need it.]",
        })
        return msg.model_copy(update={"content": kept})

    @staticmethod
    def _pairing_groups(messages):
        """Indices grouped so a tool-calling AIMessage travels with its answers.

        Dropping half a pair doesn't just loop — the provider rejects the whole
        request with "insufficient tool messages following tool_calls message".
        """
        from langchain_core.messages import ToolMessage

        groups = []
        current = None
        for i, msg in enumerate(messages):
            if isinstance(msg, ToolMessage) and current is not None:
                current.append(i)
                continue
            if getattr(msg, "tool_calls", None):
                current = [i]
                groups.append(current)
                continue
            current = None
            groups.append([i])
        return groups

    def _enforce_without_summary(self, messages, runtime=None):
        """Bound the payload even when summarization itself declined to run.

        `SummarizationMiddleware` returns None when it can't find a safe cutoff —
        a thread whose newest message is bigger than the whole budget hits that.
        The count is over the trigger, nothing changed, and the model gets the
        oversized payload anyway. Enforce the same ceiling here so an oversized
        message is never simply handed to the provider.
        """
        threshold = self._trigger_token_threshold()
        if threshold is None or not messages:
            return None
        counts = [self._count([m]) for m in messages]
        if sum(counts) < threshold:
            return None

        target = int(threshold * POST_COMPACTION_TARGET_RATIO)
        logger.error(
            "RailsSummarizationMiddleware: %d tokens are over the %d-token trigger but "
            "summarization found no safe cutoff — enforcing the ceiling directly.",
            sum(counts), threshold,
        )
        fixed = self._force_under_target(list(messages), list(counts), target)
        if len(fixed) == len(messages) and all(a is b for a, b in zip(fixed, messages)):
            return None  # nothing to do; never write a no-op update

        from langchain_core.messages import RemoveMessage
        from langgraph.graph.message import REMOVE_ALL_MESSAGES

        self._report_compaction_friction(sum(counts), threshold, messages[-1], runtime)
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *fixed]}

    @staticmethod
    def _thread_id(runtime):
        try:
            config = getattr(runtime, "config", None) or {}
            return (config.get("configurable") or {}).get("thread_id")
        except Exception:
            return None

    def _report_compaction_friction(self, total, threshold, biggest, runtime):
        """File this against our own tooling — it is invisible otherwise.

        Best-effort in every direction: telemetry must never be the reason a
        compaction fails.
        """
        try:
            from app.agents.leonardo import friction

            thread_id = self._thread_id(runtime)
            report = friction.build_friction_report(
                what_happened=(
                    f"Summarization could not get the conversation under its own trigger: "
                    f"{total} tokens remained after compaction (trigger {threshold}). "
                    f"Without force-truncation this thread re-summarizes on every step and "
                    f"never completes a turn."
                ),
                category="environment",
                severity="workaround",
                thread_id=str(thread_id) if thread_id is not None else None,
                tool_name="summarization",
                evidence=(
                    f"largest message after compaction: {type(biggest).__name__} "
                    f"id={getattr(biggest, 'id', None)} ({self._count([biggest])} tokens)"
                ),
                suggested_fix=(
                    "Find the oversized payload that reached the conversation and cap it at "
                    "ingestion (see app/websocket/payload_limits.py)."
                ),
            )
            accepted, _reason = friction.claim_report_slot(
                str(thread_id or "unknown"), report["fingerprint"],
            )
            if accepted:
                friction.dispatch_friction_report(report)
        except Exception as e:
            logger.warning(
                "Could not file friction report for uncompactable thread (non-fatal): %s", e,
            )

    @staticmethod
    def _with_appended_note(msg, note):
        content = msg.content
        if isinstance(content, str):
            new_content = content + note
        elif isinstance(content, list):
            new_content = [*content, {"type": "text", "text": note}]
        else:
            new_content = note
        return msg.model_copy(update={"content": new_content})

    @staticmethod
    def _extract_last_todos(messages):
        """Return the `todos` list from the most recent write_todos tool call, or None."""
        for m in reversed(messages):
            tool_calls = getattr(m, "tool_calls", None) or []
            for tc in tool_calls:
                name = tc.get("name") if isinstance(tc, dict) else None
                if name != "write_todos":
                    continue
                args = tc.get("args") if isinstance(tc, dict) else None
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (ValueError, TypeError):
                        args = None
                if isinstance(args, dict) and args.get("todos"):
                    return args["todos"]
        return None

    @staticmethod
    def _with_todo_restore(summary_msg, todos):
        """Append an ACTIVE TODO LIST restore instruction to the summary message."""
        try:
            todos_json = json.dumps(todos, indent=2, default=str)
        except (TypeError, ValueError):
            todos_json = str(todos)

        note = (
            "\n\n## ACTIVE TODO LIST — RESTORE IMMEDIATELY\n"
            "This was your most recent todo list before the conversation was compacted. "
            "Compaction dropped the live todo state from view. As your VERY FIRST action, "
            "call the `write_todos` tool with EXACTLY this list to restore it, then continue. "
            "Do not skip this step.\n\n"
            f"```json\n{todos_json}\n```"
        )

        content = summary_msg.content
        if isinstance(content, str):
            new_content = content + note
        elif isinstance(content, list):
            new_content = [*content, {"type": "text", "text": note}]
        else:
            new_content = note

        return HumanMessage(
            content=new_content,
            id=getattr(summary_msg, "id", None),
            additional_kwargs=dict(getattr(summary_msg, "additional_kwargs", None) or {}),
        )


def make_summarization_middleware(
    *,
    summary_prompt: str,
    keep_initial_human: int = 3,
    keep_tokens: int | None = None,
):
    """Build the shared `RailsSummarizationMiddleware` for an agent.

    Args:
        summary_prompt: the agent's summarization prompt (kept per-agent so each
            mode can tune what it extracts).
        keep_initial_human: number of leading user messages to preserve verbatim.
        keep_tokens: token budget for the preserved recent tail. Defaults to
            SUMMARIZATION_KEEP_TOKENS.
    """
    from app.agents.leonardo.llm_factory import make_summarization_model
    from app.agents.utils.token_counter import (
        SUMMARIZATION_KEEP_TOKENS,
        SUMMARIZATION_TOKEN_THRESHOLD,
    )

    model, token_counter, trim_tokens_to_summarize = make_summarization_model()

    return RailsSummarizationMiddleware(
        model=model,
        trigger=("tokens", SUMMARIZATION_TOKEN_THRESHOLD),
        keep=("tokens", keep_tokens or SUMMARIZATION_KEEP_TOKENS),
        token_counter=_strip_images_then_count(token_counter),
        trim_tokens_to_summarize=trim_tokens_to_summarize,
        summary_prompt=summary_prompt,
        keep_initial_human=keep_initial_human,
    )
