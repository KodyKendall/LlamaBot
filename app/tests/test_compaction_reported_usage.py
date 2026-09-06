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


def _convo(n_pairs, last_ai_usage=None):
    """n_pairs of (human, ai); the last AI optionally reports provider usage."""
    msgs = []
    for i in range(n_pairs):
        msgs.append(HumanMessage(content=f"user turn {i}", id=f"h{i}"))
        kwargs = {}
        if last_ai_usage is not None and i == n_pairs - 1:
            kwargs["usage_metadata"] = last_ai_usage
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


def test_calibration_ratio_is_reported_over_estimated_at_that_message():
    msgs = _convo(3, last_ai_usage=_usage(17_900))  # 18_000 reported over 6 messages
    assert calibration_ratio(msgs, _per_message_counter) == 3.0


def test_calibration_never_shrinks_the_estimate_and_is_capped():
    assert calibration_ratio(_convo(3, last_ai_usage=_usage(900)), _per_message_counter) == 1.0
    assert calibration_ratio(_convo(3), _per_message_counter) == 1.0
    huge = _convo(3, last_ai_usage=_usage(6_000_000))
    assert calibration_ratio(huge, _per_message_counter) == MAX_CALIBRATION_RATIO


def test_calibration_sizes_the_kept_tail_in_real_tokens():
    """keep=2000: by the estimate that is two messages. If the provider says
    every token is really two, the kept tail must be one message — otherwise
    a compaction lands at 2x the intended size and re-fires next call."""
    uncalibrated = _mw().before_model({"messages": _convo(6)}, None)  # 12 msgs, est 12k
    assert len(_tail(uncalibrated)) == 2
    calibrated = _mw().before_model(
        {"messages": _convo(6, last_ai_usage=_usage(23_900))}, None  # ratio 2
    )
    assert len(_tail(calibrated)) == 1


def test_a_pathological_ratio_still_compacts_and_keeps_the_summary():
    """A tiny estimate under a huge reported count must not wreck the summary."""
    msgs = _convo(6, last_ai_usage=_usage(6_000_000))
    result = _mw().before_model({"messages": msgs}, None)
    assert result is not None
    kept = _kept(result)
    summary = next(
        m for m in kept
        if isinstance(m, HumanMessage) and (m.additional_kwargs or {}).get("lc_source") == "summarization"
    )
    assert "FIXED SUMMARY TEXT" in summary.content
