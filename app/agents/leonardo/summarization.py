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

logger = logging.getLogger(__name__)


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
            return None
        return self._augment(original, result)

    async def abefore_model(self, state, runtime):
        original = list(state["messages"])
        result = await super().abefore_model(state, runtime)
        if result is None:
            return None
        return self._augment(original, result)

    # -- augmentation ---------------------------------------------------------

    def _augment(self, original_messages, result):
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

        return {"messages": [remove_op, *initial, summary_msg, *preserved]}

    def _first_human_messages(self, messages, exclude_ids):
        """First `keep_initial_human` real user messages, excluding the tail set."""
        out = []
        for m in messages:
            if len(out) >= self.keep_initial_human:
                break
            if not isinstance(m, HumanMessage):
                continue
            if (getattr(m, "additional_kwargs", None) or {}).get("lc_source") == "summarization":
                continue
            if getattr(m, "id", None) in exclude_ids:
                continue
            out.append(m)
        return out

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
