"""Compaction must trigger on the provider's REAL token count, for any model.

Before this, the 150k trigger was a local tiktoken estimate of the checkpoint
state. LangChain's own "trust the reported usage" path is gated on the
summarizer's provider matching the chat model's provider — which is never true
here, because the summarizer is picked by which API key exists (DeepSeek first)
while the chat model is whatever the user chose. Muse Spark threads reached
600k tokens with the estimate still under the trigger.

The contract now:

- the last AI message's ``usage_metadata`` (input + output) plus the estimate
  of whatever came after it IS the context size; the trigger uses the larger
  of that and the estimate, regardless of provider;
- a compaction clears ``usage_metadata`` from the AI messages it keeps, so the
  stale pre-compaction number can never re-fire the trigger on the next call.

No real LLM: a fake summarizer returns a fixed string.
"""
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.leonardo.summarization import (
    MAX_CALIBRATION_RATIO,
    RailsSummarizationMiddleware,
    calibration_ratio,
    reported_context_tokens,
)


class _FakeSummaryModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "fake-summary"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="FIXED SUMMARY TEXT"))]
        )


def _per_message_counter(messages):
    """1000 'tokens' per message — deterministic and nothing like the real count."""
    return 1000 * len(list(messages))


TRIGGER = 10_000  # ten messages by the estimate


def _mw():
    return RailsSummarizationMiddleware(
        model=_FakeSummaryModel(),
        trigger=("tokens", TRIGGER),
        keep=("tokens", 2000),
        token_counter=_per_message_counter,
        trim_tokens_to_summarize=None,
        summary_prompt="Summarize:\n{messages}",
        keep_initial_human=1,
    )


def _usage(input_tokens, output_tokens=100):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _convo(n_pairs, last_ai_usage=None, usages=None):
    """n_pairs of (human, ai); the last AI optionally reports provider usage.

    ``usages`` maps a pair index to the usage its AI message reports, for
    threads with more than one provider report.
    """
    usages = dict(usages or {})
    if last_ai_usage is not None:
        usages[n_pairs - 1] = last_ai_usage
    msgs = []
    for i in range(n_pairs):
        msgs.append(HumanMessage(content=f"user turn {i}", id=f"h{i}"))
        kwargs = {}
        if i in usages:
            kwargs["usage_metadata"] = usages[i]
        msgs.append(
            AIMessage(
                content=f"assistant turn {i}",
                id=f"a{i}",
                # A provider the fake summarizer can never "match".
                response_metadata={"model_provider": "openai", "model_name": "muse-spark"},
                **kwargs,
            )
        )
    return msgs


def _kept(result):
    return [m for m in result["messages"] if not isinstance(m, RemoveMessage)]


def test_estimate_alone_stays_under_the_trigger():
    """Baseline: 3 pairs = 6 messages = 6000 estimated tokens, no compaction."""
    assert _mw().before_model({"messages": _convo(3)}, None) is None


def test_reported_usage_triggers_compaction_when_the_estimate_is_under():
    """Same 6 messages, but the provider says the last call cost 12k: compact."""
    msgs = _convo(3, last_ai_usage=_usage(11_900))
    result = _mw().before_model({"messages": msgs}, None)
    assert result is not None, "provider-reported usage over the trigger must compact"
    assert isinstance(result["messages"][0], RemoveMessage)


def test_reported_usage_under_the_trigger_does_not_compact():
    msgs = _convo(3, last_ai_usage=_usage(4_000))
    assert _mw().before_model({"messages": msgs}, None) is None


def test_reported_context_is_last_ai_usage_plus_everything_after_it():
    msgs = _convo(2, last_ai_usage=_usage(4_000, output_tokens=500))
    msgs.append(ToolMessage(content="tool result", tool_call_id="x", id="t1"))
    msgs.append(ToolMessage(content="tool result 2", tool_call_id="y", id="t2"))
    # 4000 in + 500 out + two later messages at 1000 each by the estimate.
    assert reported_context_tokens(msgs, _per_message_counter) == 6_500


def test_no_reported_usage_means_no_reported_context():
    assert reported_context_tokens(_convo(2), _per_message_counter) is None


def test_compaction_clears_stale_usage_so_it_cannot_retrigger():
    """The kept tail was produced against the OLD context; its usage is stale.

    If it survived, the very next before_model would read 12k again and
    compact again — the summarize-on-every-turn loop by another door.
    """
    msgs = _convo(3, last_ai_usage=_usage(11_900))
    mw = _mw()
    first = mw.before_model({"messages": msgs}, None)
    assert first is not None
    kept = _kept(first)
    stale = [m for m in kept if isinstance(m, AIMessage) and m.usage_metadata]
    assert not stale, f"kept AI messages still carry pre-compaction usage: {stale}"
    # And the loop really is closed: the compacted state does not re-fire.
    assert mw.before_model({"messages": kept}, None) is None


@pytest.mark.asyncio
async def test_async_path_honors_reported_usage_too():
    msgs = _convo(3, last_ai_usage=_usage(11_900))
    mw = _mw()
    result = await mw.abefore_model({"messages": msgs}, None)
    assert result is not None
    kept = _kept(result)
    assert not [m for m in kept if isinstance(m, AIMessage) and m.usage_metadata]
    assert await mw.abefore_model({"messages": kept}, None) is None


def _tail(result):
    """Messages kept after the summary (the recent tail)."""
    kept = _kept(result)
    idx = next(
        i for i, m in enumerate(kept)
        if isinstance(m, HumanMessage) and (m.additional_kwargs or {}).get("lc_source") == "summarization"
    )
    return kept[idx + 1:]


# Calibration is affine: reported = fixed overhead (system prompt + tool
# schemas, ~42k on rails_agent) + ratio * estimate. The estimate never sees the
# overhead, so reported / estimated on a short thread is mostly overhead — it
# pinned at 8x on every new thread and fired compaction on the FIRST message
# (crm-4, 2026-09-21). The ratio is the GROWTH between two reports over the
# growth of the estimate between them; the overhead cancels out.

def test_calibration_ratio_is_growth_between_reports_over_estimate_growth():
    # est 2000 at a0, 6000 at a2; reported grows 10_000 -> 22_000: ratio 3.
    msgs = _convo(3, usages={0: _usage(9_900), 2: _usage(21_900)})
    assert calibration_ratio(msgs, _per_message_counter) == 3.0


def test_one_report_is_mostly_fixed_overhead_so_calibrates_at_one():
    """THE bug: 18_000 reported over a 6_000 estimate is overhead, not a 3x tokenizer."""
    assert calibration_ratio(_convo(3, last_ai_usage=_usage(17_900)), _per_message_counter) == 1.0


def test_calibration_never_shrinks_the_estimate_and_is_capped():
    assert calibration_ratio(_convo(3, usages={0: _usage(9_900), 2: _usage(9_950)}), _per_message_counter) == 1.0
    assert calibration_ratio(_convo(3), _per_message_counter) == 1.0
    huge = _convo(3, usages={0: _usage(900), 2: _usage(6_000_000)})
    assert calibration_ratio(huge, _per_message_counter) == MAX_CALIBRATION_RATIO


def test_calibration_sizes_the_kept_tail_in_real_tokens():
    """keep=2000: by the estimate that is two messages. If the provider says
    every token is really two, the kept tail must be one message — otherwise
    a compaction lands at 2x the intended size and re-fires next call."""
    uncalibrated = _mw().before_model({"messages": _convo(6)}, None)  # 12 msgs, est 12k
    assert len(_tail(uncalibrated)) == 2
    calibrated = _mw().before_model(
        # est 2000 at a0, 12_000 at a5; reported grows 5_000 -> 25_000: ratio 2.
        {"messages": _convo(6, usages={0: _usage(4_900), 5: _usage(24_900)})}, None
    )
    assert len(_tail(calibrated)) == 1


def test_a_pathological_ratio_still_compacts_and_keeps_the_summary():
    """A tiny estimate under a huge reported count must not wreck the summary."""
    msgs = _convo(6, usages={0: _usage(1_000), 5: _usage(6_000_000)})
    result = _mw().before_model({"messages": msgs}, None)
    assert result is not None
    kept = _kept(result)
    summary = next(
        m for m in kept
        if isinstance(m, HumanMessage) and (m.additional_kwargs or {}).get("lc_source") == "summarization"
    )
    assert "FIXED SUMMARY TEXT" in summary.content


# --- first message, one big tool result (crm-4, 2026-09-21) --------------------

def _chars_counter(messages):
    """~4 chars a token, like tiktoken on English and code."""
    return sum(len(str(m.content)) // 4 + 4 for m in messages)


def _real_mw():
    return RailsSummarizationMiddleware(
        model=_FakeSummaryModel(),
        trigger=("tokens", 150_000),
        keep=("tokens", 30_000),
        token_counter=_chars_counter,
        trim_tokens_to_summarize=None,
        summary_prompt="Summarize:\n{messages}",
        keep_initial_human=1,
    )


def _read_schema_turn(input_tokens=42_000):
    return [
        HumanMessage(content="add Heitor as a new user", id="h0"),
        AIMessage(
            content="",
            id="a0",
            tool_calls=[{"name": "read_file", "args": {"path": "db/schema.rb"}, "id": "c0"}],
            usage_metadata=_usage(input_tokens),
        ),
        ToolMessage(content="x" * 73_000, tool_call_id="c0", id="t0"),
    ]


def test_first_message_plus_a_big_tool_result_does_not_compact():
    """42k of prompt overhead + ~18k of schema.rb is ~60k real tokens, far under
    150k. It compacted because the one report calibrated at 8x and the schema was
    then counted at 8x on top of the provider figure."""
    assert _real_mw().before_model({"messages": _read_schema_turn()}, None) is None


def test_the_reported_figure_counts_its_tail_raw_not_calibrated():
    """The provider figure already covers everything up to its message; the
    tail after it is plain text the estimate reads well. Scaling the tail by
    the calibration ratio on top of the provider figure double-counts."""
    from app.agents.leonardo.summarization import _CALIBRATION

    msgs = _read_schema_turn()  # 42_100 reported + ~18k raw tail
    mw = _real_mw()
    token = _CALIBRATION.set(MAX_CALIBRATION_RATIO)
    try:
        # total_tokens=0 keeps the stock estimate path out of it: only the
        # provider-reported figure can fire. At 8x the tail alone was ~146k.
        assert mw._should_summarize(msgs, 0) is False
    finally:
        _CALIBRATION.reset(token)


def test_a_long_thread_the_estimate_undercounts_still_compacts():
    """The 2026-09-03 Muse fix must hold: provider 180k, estimate ~60k."""
    msgs = _read_schema_turn(input_tokens=180_000)[:2]
    msgs[1] = AIMessage(content="done", id="a0", usage_metadata=_usage(180_000))
    msgs.insert(1, HumanMessage(content="y" * 240_000, id="h1"))
    assert _real_mw().before_model({"messages": msgs}, None) is not None
