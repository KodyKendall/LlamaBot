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

import contextvars
import json
import logging
import re

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, get_buffer_string

from app.lib.text_budget import shrink_to_token_budget

logger = logging.getLogger(__name__)

# --- Model independence --------------------------------------------------------
#
# Two things used to tie compaction to a specific model, and together they let a
# Muse Spark thread reach 600k tokens with the trigger never firing:
#
# 1. The trigger was a local tiktoken ESTIMATE of the state. The provider's real
#    `input_tokens` was never consulted — stock SummarizationMiddleware only
#    trusts reported usage when the summarizer's provider matches the chat
#    model's, and ours never match (the summarizer is picked by API key).
#    `reported_context_tokens` makes the provider's number the trigger's floor
#    for every model.
# 2. The summarizer was whichever provider had a key on the box. On a box
#    funded for one provider, compaction depended on a different one; when it
#    failed, the literal "Error generating summary: ..." was stored as memory.
#    The model in use is now asked first; the key chain is the fallback; total
#    failure stores an honest notice, never an exception.

# The chat model of the run currently being compacted (state["llm_model"]).
# A contextvar rather than an instance attribute because one middleware
# instance serves every concurrent run of its agent.
_ACTIVE_CHAT_MODEL: contextvars.ContextVar = contextvars.ContextVar(
    "leonardo_active_chat_model", default=None
)

_SUMMARY_CONFIG = {"metadata": {"lc_source": "summarization"}}

# Calibration: reported / estimated, for the run being compacted. Every count
# the middleware makes (trigger, keep-tail cutoff, loop guards, summary trim)
# goes through `_calibrated_counter`, which multiplies the local estimate by
# this. So a model whose real tokens run 3x the tiktoken estimate keeps a 30k
# REAL tail, not a 90k one, and lands under the real trigger after compaction.
_CALIBRATION: contextvars.ContextVar = contextvars.ContextVar(
    "leonardo_token_calibration", default=1.0
)

# A ratio above this says the estimate cannot see most of the context (a video
# attachment, a provider counting something we do not store). Scaling a
# handful of tiny messages by 1000x would then force-truncate the summary
# itself, which is worse than compacting a little too gently: the next call
# reports fresh usage and compaction runs again.
MAX_CALIBRATION_RATIO = 8.0


def _calibrated_counter(counter):
    def _count(messages):
        ratio = _CALIBRATION.get()
        raw = counter(messages)
        return raw if ratio == 1.0 else int(round(raw * ratio))

    return _count


def calibration_ratio(messages, raw_counter):
    """reported / estimated at the newest AI message that reported usage.

    1.0 when nothing was reported, when the estimate is zero, or when the
    estimate already over-counts; never above ``MAX_CALIBRATION_RATIO``.
    """
    msgs = list(messages or [])
    for idx in range(len(msgs) - 1, -1, -1):
        m = msgs[idx]
        if not isinstance(m, AIMessage):
            continue
        usage = getattr(m, "usage_metadata", None) or {}
        try:
            reported = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if reported <= 0:
            continue
        try:
            estimated = int(raw_counter(msgs[: idx + 1]))
        except Exception:  # noqa: BLE001 - an estimate must never break a turn
            return 1.0
        if estimated <= 0:
            return 1.0
        return max(1.0, min(MAX_CALIBRATION_RATIO, reported / estimated))
    return 1.0

SUMMARY_UNAVAILABLE_TEXT = (
    "Summary unavailable: every summarizer model failed, so the earlier part of "
    "this conversation was dropped WITHOUT a summary. Nothing from before this "
    "point is known to you. If you need something from earlier, re-read the "
    "relevant files or ask the user to restate it. Do not guess."
)


def reported_context_tokens(messages, token_counter):
    """The context size the provider actually saw, or None if it never said.

    The newest AI message carrying ``usage_metadata`` is the truth for
    everything up to and including itself — system prompt, tool schemas and
    the model's own tokenizer included. Whatever came after it (tool results,
    the user's next message) is added by estimate. Provider-agnostic: no
    check on who reported the number, because the number is what got billed.

    Compaction clears ``usage_metadata`` from the AI messages it keeps
    (``_clear_reported_usage``), so a pre-compaction figure can never fire
    the trigger again on the next call.
    """
    msgs = list(messages or [])
    for idx in range(len(msgs) - 1, -1, -1):
        m = msgs[idx]
        if not isinstance(m, AIMessage):
            continue
        usage = getattr(m, "usage_metadata", None) or {}
        try:
            inp = int(usage.get("input_tokens") or 0)
            out = int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if inp <= 0:
            continue
        tail = msgs[idx + 1:]
        try:
            after = int(token_counter(tail)) if tail else 0
        except Exception:  # an estimate must never be the thing that breaks a turn
            after = 0
        return inp + out + after
    return None

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

# A message larger than the preserved tail can never be compacted away. When one
# is found it is fitted to this fraction of the keep budget — small enough that
# the tail still holds a real conversation around it, big enough that the content
# is not reduced to a stub.
BALLAST_FIT_RATIO = 0.5

_TRUNCATION_NOTICE = (
    "\n\n## NOTE: OVERSIZED CONTENT WAS TRUNCATED\n"
    "This conversation contains content too large to keep in context (usually a very "
    "large page element pasted in by the element picker, or a huge page). It was "
    "truncated so this thread can keep working. If you need that content, re-read the "
    "relevant file or page directly instead of relying on the conversation history, and "
    "tell the user that some earlier content was dropped — if the thread keeps "
    "struggling, suggest they start a new one."
)


# A summarizer that emits a tool call instead of prose stores markup as the
# thread's summary. In the 2026-08-13 incident the stored summary was
# `<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="read_file">...` — so the agent
# re-derived context it already had, generated more messages, and fed the loop it
# was supposed to end. Matched loosely on purpose: every provider spells its
# tool-call markup differently and a false positive only costs one summary.
_TOOL_CALL_MARKUP_RE = re.compile(
    r"DSML"
    r"|<\s*(function|tool)[_\s]*call"
    r"|<\s*invoke\s+name\s*="
    r"|<\|[^|]*tool[^|]*\|>"
    r"|<\s*antml:",
    re.IGNORECASE,
)

_CORRUPT_SUMMARY_REPLACEMENT = (
    "The automatic summary of this conversation could not be produced (the "
    "summarization model returned a tool call instead of a summary). Earlier "
    "history has been compacted away and is not recoverable from this "
    "conversation. Do NOT guess at what was discussed: if you need earlier "
    "context, re-read the relevant files directly, and ask the user to confirm "
    "anything you are unsure about."
)


def looks_like_tool_call_markup(text) -> bool:
    """True if `text` is a model's tool-call syntax rather than prose."""
    return isinstance(text, str) and bool(_TOOL_CALL_MARKUP_RE.search(text))


def validate_summary_text(text):
    """Return `text`, or a safe replacement if it isn't actually a summary.

    Storing markup as the summary is worse than storing nothing: it is carried
    forward by every future compaction, it tells the agent nothing, and it reads
    to the model as an instruction to go call a tool.
    """
    if not looks_like_tool_call_markup(text):
        return text
    logger.error(
        "RailsSummarizationMiddleware: the summarization model returned tool-call "
        "markup instead of a summary (%d chars, starts %r). Storing a placeholder "
        "instead — a corrupt summary makes the agent re-derive context it already "
        "had and feeds the compaction loop.",
        len(text), text[:120],
    )
    return _CORRUPT_SUMMARY_REPLACEMENT


def _record_compaction_on_turn():
    """Count this compaction against the turn the user is waiting on.

    The request handler reads the count when the turn ends and tells the user in
    chat if the thread has been compacting instead of working. Best-effort in
    every direction: there is no recorder installed in headless runs or tests,
    and telemetry must never be the reason a compaction fails.
    """
    try:
        from app.lib.turn_metrics import current_turn

        turn = current_turn()
        if turn is not None:
            turn.record_compaction()
    except Exception:  # pragma: no cover - defensive
        pass


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
        # The estimate as given; every count the middleware makes is the
        # calibrated view of it (see _CALIBRATION).
        self._raw_counter = self.token_counter
        self.token_counter = _calibrated_counter(self._raw_counter)
        # Stock keeps a second counter for the keep-tail binary search; it must
        # see the same calibrated numbers or the tail is sized in estimate tokens.
        self._partial_token_counter = self.token_counter

    # -- hooks ----------------------------------------------------------------

    def _enter(self, state):
        original = list(state["messages"])
        tokens = (
            _ACTIVE_CHAT_MODEL.set(self._chat_model_of(state)),
            _CALIBRATION.set(calibration_ratio(original, self._raw_counter)),
        )
        ratio = _CALIBRATION.get()
        if ratio != 1.0:
            logger.info(
                "RailsSummarizationMiddleware: provider usage runs %.2fx the local "
                "estimate; counting in calibrated tokens for this compaction check.",
                ratio,
            )
        return original, tokens

    @staticmethod
    def _exit(tokens):
        model_token, calibration_token = tokens
        _ACTIVE_CHAT_MODEL.reset(model_token)
        _CALIBRATION.reset(calibration_token)

    def before_model(self, state, runtime):
        original, tokens = self._enter(state)
        try:
            result = super().before_model(state, runtime)
            if result is None:
                result = self._enforce_without_summary(original, runtime)
            else:
                result = self._augment(original, result, runtime)
        finally:
            self._exit(tokens)
        return self._clear_reported_usage(result)

    async def abefore_model(self, state, runtime):
        original, tokens = self._enter(state)
        try:
            result = await super().abefore_model(state, runtime)
            if result is None:
                result = self._enforce_without_summary(original, runtime)
            else:
                result = self._augment(original, result, runtime)
        finally:
            self._exit(tokens)
        return self._clear_reported_usage(result)

    # -- provider-reported usage ---------------------------------------------

    def _should_summarize(self, messages, total_tokens):
        """Trigger on the larger of the estimate and the provider's own count."""
        reported = reported_context_tokens(messages, self._count)
        if reported is not None and reported > total_tokens:
            logger.info(
                "RailsSummarizationMiddleware: provider reports %d tokens of context "
                "(local estimate %d); using the reported figure for the trigger.",
                reported, total_tokens,
            )
            total_tokens = reported
        return super()._should_summarize(messages, total_tokens)

    @staticmethod
    def _clear_reported_usage(result):
        """Strip usage from the AI messages a compaction keeps.

        They were produced against the OLD context; carrying their
        ``usage_metadata`` forward would re-fire the trigger on the very next
        call — the summarize-on-every-turn loop by another door.
        """
        if not result or not result.get("messages"):
            return result
        cleared = []
        for m in result["messages"]:
            if isinstance(m, AIMessage) and getattr(m, "usage_metadata", None):
                m = m.model_copy(update={"usage_metadata": None})
            cleared.append(m)
        return {**result, "messages": cleared}

    # -- which model summarizes ----------------------------------------------

    @staticmethod
    def _chat_model_of(state):
        try:
            return state.get("llm_model") or None
        except Exception:
            return None

    def _summarizer_candidates(self):
        """The model in use first, then the key-chain fallback (``self.model``)."""
        chat_model = _ACTIVE_CHAT_MODEL.get()
        if chat_model:
            try:
                from app.agents.leonardo import llm_factory

                yield f"chat model {chat_model}", llm_factory.get_llm(chat_model)
            except Exception as e:  # noqa: BLE001 - fall through to the chain
                logger.warning(
                    "RailsSummarizationMiddleware: could not build the chat model %r "
                    "as summarizer (%s); using the fallback chain.", chat_model, e,
                )
        yield "fallback chain", self.model

    def _summary_prompt_for(self, messages_to_summarize):
        """(prompt, early_return): the stock preamble, factored so both paths share it."""
        if not messages_to_summarize:
            return None, "No previous conversation history."
        trimmed = self._trim_messages_for_summary(messages_to_summarize)
        if not trimmed:
            return None, "Previous conversation was too long to summarize."
        return self.summary_prompt.format(messages=get_buffer_string(trimmed)).rstrip(), None

    @staticmethod
    def _summary_unavailable():
        logger.error(
            "RailsSummarizationMiddleware: every summarizer failed; compacting WITHOUT "
            "a summary so the thread stays under the context limit."
        )
        return SUMMARY_UNAVAILABLE_TEXT

    def _create_summary(self, messages_to_summarize):
        prompt, early = self._summary_prompt_for(messages_to_summarize)
        if early is not None:
            return early
        for label, model in self._summarizer_candidates():
            try:
                text = (model.invoke(prompt, config=_SUMMARY_CONFIG).text or "").strip()
            except Exception as e:  # noqa: BLE001 - try the next summarizer
                logger.warning("RailsSummarizationMiddleware: %s failed to summarize: %s", label, e)
                continue
            if text:
                return text
            logger.warning("RailsSummarizationMiddleware: %s returned an empty summary", label)
        return self._summary_unavailable()

    async def _acreate_summary(self, messages_to_summarize):
        prompt, early = self._summary_prompt_for(messages_to_summarize)
        if early is not None:
            return early
        for label, model in self._summarizer_candidates():
            try:
                text = ((await model.ainvoke(prompt, config=_SUMMARY_CONFIG)).text or "").strip()
            except Exception as e:  # noqa: BLE001 - try the next summarizer
                logger.warning("RailsSummarizationMiddleware: %s failed to summarize: %s", label, e)
                continue
            if text:
                return text
            logger.warning("RailsSummarizationMiddleware: %s returned an empty summary", label)
        return self._summary_unavailable()

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

        summary_msg = self._validate_summary(body[summary_idx])
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
        _record_compaction_on_turn()
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
        """Guarantee the compacted thread lands under the post-compaction TARGET.

        If a compaction lands back above the trigger, `before_model` fires again
        on the very next step and the agent spends every turn summarizing instead
        of thinking — the thread is dead and the user only sees a spinner. That
        happens whenever a single message is bigger than the whole budget (stock
        `SummarizationMiddleware` never summarizes away the current turn's
        message, so it is always in the preserved tail).

        Why the TARGET and not the trigger
        ----------------------------------
        This guard used to return happy at `total < threshold`, and that is how
        the 2026-08-13 incident stayed invisible for 18 minutes. A 74k-token
        `grep_files` result — bigger than the 30k keep-tail, so uncompactable by
        construction — left every pass landing at ~90-101k against a 150k
        trigger. Under the trigger, so the guard said fine and logged nothing;
        but with only ~49k of headroom against permanent ballast, two or three
        ordinary steps crossed 150k again and it re-summarized. Forever.

        A compaction that does not get under `POST_COMPACTION_TARGET_RATIO` of
        the trigger has not reclaimed enough to buy a turn's work, so it IS the
        loop, whichever side of the trigger it landed on. Treat it as one: fail
        loud, file the friction report, and force the count under the target
        instead of handing back a state we know will re-fire.
        """
        threshold = self._trigger_token_threshold()
        if threshold is None:
            return messages

        target = int(threshold * POST_COMPACTION_TARGET_RATIO)
        counts = [self._count([m]) for m in messages]

        # Permanent ballast first: a single message bigger than the keep-tail
        # survives every future compaction, so the total being fine today says
        # nothing about tomorrow. This is the check that actually catches the
        # 2026-08-13 shape — that thread compacted to ~101k against a 150k
        # trigger, comfortably under BOTH the trigger and the 120k target, and
        # was still doomed, because 74k of what remained could never be removed.
        messages, counts, shrank_ballast = self._shrink_permanent_ballast(
            messages, counts, threshold, runtime,
        )

        total = sum(counts)
        if total <= target:
            # Content was still dropped if ballast was cut, and the agent has to
            # know that so it can tell the user rather than answering confidently
            # from a conversation with a hole in it.
            return self._note_truncation(messages) if shrank_ballast else messages

        biggest = max(range(len(messages)), key=lambda i: counts[i])
        logger.error(
            "RailsSummarizationMiddleware: compaction did not reclaim enough — %d tokens "
            "remain against a post-compaction target of %d (trigger %d). This thread "
            "re-summarizes every few steps and never completes a turn. Force-truncating "
            "to %d tokens. Largest contributor: %s id=%s (%d tokens).",
            total, target, threshold, target, type(messages[biggest]).__name__,
            getattr(messages[biggest], "id", None), counts[biggest],
        )

        out = self._note_truncation(self._force_under_target(list(messages), counts, target))
        self._report_compaction_friction(total, threshold, messages[biggest], runtime)
        return out

    @staticmethod
    def _validate_summary(summary_msg):
        """Never store tool-call markup as a thread's summary."""
        content = getattr(summary_msg, "content", None)
        if not isinstance(content, str):
            return summary_msg
        checked = validate_summary_text(content)
        if checked is content:
            return summary_msg
        return summary_msg.model_copy(update={"content": checked})

    def _note_truncation(self, messages):
        """Tell the agent content was dropped, so it can tell the user."""
        return [
            self._with_appended_note(m, _TRUNCATION_NOTICE) if self._is_summary(m) else m
            for m in messages
        ]

    def _keep_token_budget(self):
        """Tokens the middleware preserves as the recent tail, if token-based."""
        keep = getattr(self, "keep", None)
        if isinstance(keep, (tuple, list)) and len(keep) == 2 and keep[0] == "tokens":
            return int(keep[1])
        return None

    def _shrink_permanent_ballast(self, messages, counts, threshold, runtime):
        """Fit any message too big for the preserved tail to ever shed it.

        `SummarizationMiddleware` never splits a tool-call group and never
        summarizes the preserved tail, so a single message larger than the
        keep-tail budget is immortal: it is re-preserved by every compaction for
        the rest of the thread's life. Each pass then reclaims only the ordinary
        history around it, buys a step or two of headroom, and re-fires. The
        totals look healthy the whole time — the thread just never finishes a
        turn, and the user sees a spinner.

        Nothing should reach here now that tool results are capped at source
        (`app/agents/utils/tool_output_limits.py`) and inbound frames before that
        (`app/websocket/payload_limits.py`). That is the point: this is the
        backstop for the next unbounded string we have not thought of, and it is
        loud so we find out about it from telemetry rather than from a customer.
        """
        keep_budget = self._keep_token_budget()
        if keep_budget is None:
            return messages, counts, False

        if all(c <= keep_budget for c in counts):
            return messages, counts, False

        allowance = max(MIN_PRESERVE_TOKENS, int(keep_budget * BALLAST_FIT_RATIO))
        messages = list(messages)
        counts = list(counts)
        for i, msg in enumerate(list(messages)):
            if counts[i] <= keep_budget:
                continue
            fitted = self._fit_message(msg, allowance)
            fitted_cost = self._count([fitted])
            logger.error(
                "RailsSummarizationMiddleware: message %s id=%s is %d tokens — larger "
                "than the %d-token preserved tail, so no future compaction can ever "
                "remove it and this thread would re-summarize every few steps forever. "
                "Truncating it to %d tokens. Something produced an uncapped payload; "
                "find it and cap it at the source.",
                type(msg).__name__, getattr(msg, "id", None), counts[i],
                keep_budget, fitted_cost,
            )
            self._report_compaction_friction(sum(counts), threshold, msg, runtime)
            if fitted_cost < counts[i]:
                messages[i] = fitted
                counts[i] = fitted_cost
        return messages, counts, True

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
        _record_compaction_on_turn()
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
            target = int(threshold * POST_COMPACTION_TARGET_RATIO)
            report = friction.build_friction_report(
                what_happened=(
                    f"Summarization did not reclaim enough to buy a turn's work: "
                    f"{total} tokens remained after compaction, against a post-compaction "
                    f"target of {target} (trigger {threshold}). Without force-truncation "
                    f"this thread re-summarizes every few steps and never completes a turn."
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
                    "Find the oversized payload that reached the conversation and cap it "
                    "where it enters: app/websocket/payload_limits.py for anything the "
                    "browser sent, app/agents/utils/tool_output_limits.py for anything a "
                    "tool produced."
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


# ---------------------------------------------------------------------------
# Context management for RAW StateGraph nodes
# ---------------------------------------------------------------------------
#
# `create_agent` modes get compaction for free: they pass
# `make_summarization_middleware()` into the middleware stack and LangGraph runs
# `before_model` for them. A raw `StateGraph` node — one that calls
# `llm.invoke(messages)` itself — runs NO middleware, so nothing ever trims it
# and its history climbs until the provider rejects the turn (2026-08-23 fleet
# telemetry: `rails_beginner_agent` p90 655k tokens, 32 of 33 all-time
# 1,048,576-token crashes).
#
# This is deliberately a thin adapter over the SAME middleware, not a second
# implementation of the rule — two implementations of one rule is how beginner
# mode ended up without it.

_COMPACTOR_CACHE: dict = {}


def _compactor(summary_prompt: str, keep_initial_human: int):
    key = (summary_prompt, keep_initial_human)
    mw = _COMPACTOR_CACHE.get(key)
    if mw is None:
        mw = make_summarization_middleware(
            summary_prompt=summary_prompt,
            keep_initial_human=keep_initial_human,
        )
        _COMPACTOR_CACHE[key] = mw
    return mw


def compact_messages_if_needed(
    messages,
    *,
    summary_prompt: str,
    keep_initial_human: int = 3,
    runtime=None,
    llm_model: str | None = None,
):
    """Compact a raw ``StateGraph`` node's conversation before it hits the model.

    Pass ``state["messages"]`` — the conversation only, NOT the system message
    or any per-turn notes the node appends, which are rebuilt every turn and
    must not be summarized away. Pass ``llm_model`` (``state["llm_model"]``) so
    the model in use writes the summary, exactly as the middleware path does.

    Returns ``(messages_for_the_model, ops_to_persist)``:

    - ``messages_for_the_model`` is what to hand the provider this turn.
    - ``ops_to_persist`` is the message-channel write that makes the compaction
      stick (``[RemoveMessage(REMOVE_ALL_MESSAGES), summary, *tail]``, which the
      ``DeltaChannel`` reducer in ``app/agents/utils/delta_state.py`` honors).
      Merge it into the node's return value ahead of the model response, or the
      node pays for a fresh summarization on every single model call.

    Both are empty-safe: when nothing needs doing the original list comes back
    and ``ops_to_persist`` is ``[]``. Compaction failing must never be the thing
    that kills a turn, so any exception falls through to the uncompacted list.
    """
    msgs = list(messages or [])
    if not msgs:
        return msgs, []

    try:
        result = _compactor(summary_prompt, keep_initial_human).before_model(
            {"messages": msgs, "llm_model": llm_model}, runtime
        )
    except Exception:
        logger.exception(
            "Context compaction failed; proceeding with the uncompacted history."
        )
        return msgs, []

    ops = list((result or {}).get("messages") or [])
    if not ops:
        return msgs, []

    kept = [m for m in ops if not isinstance(m, RemoveMessage)]
    return kept, ops
