"""The summarizer is the model you are chatting with; the key chain is the fallback.

``make_summarization_model()`` picks a summarizer by which API key exists on the
box, not by the model in use. On a box configured for one provider that made
compaction depend on a *different* provider being funded and reachable — and
when it was not, LangChain stored the literal string
``"Error generating summary: ..."`` as the thread's memory.

Contract:

1. ``state["llm_model"]`` (via ``get_llm``) is asked for the summary first.
2. If that raises, returns nothing, or ``get_llm`` itself fails, the
   configured fallback model (the key chain) is used.
3. If every summarizer fails, the stored summary says so plainly and never
   contains an error string dressed up as context.
4. ``compact_messages_if_needed`` (raw StateGraph nodes) takes ``llm_model``
   and threads it through the same path.
"""
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import app.agents.leonardo.llm_factory as llm_factory
import app.agents.leonardo.summarization as summarization
from app.agents.leonardo.summarization import (
    RailsSummarizationMiddleware,
    compact_messages_if_needed,
)


class _Recorder(BaseChatModel):
    """A chat model that returns `reply`, or raises if `reply` is None."""

    reply: str | None = None
    calls: int = 0

    @property
    def _llm_type(self):
        return "recorder"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.reply is None:
            raise RuntimeError("summarizer down")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _counter(messages):
    return 1000 * len(list(messages))


def _mw(fallback):
    return RailsSummarizationMiddleware(
        model=fallback,
        trigger=("tokens", 5000),
        keep=("tokens", 2000),
        token_counter=_counter,
        trim_tokens_to_summarize=None,
        summary_prompt="Summarize:\n{messages}",
        keep_initial_human=1,
    )


def _convo(n=8):
    msgs = []
    for i in range(n):
        msgs.append(HumanMessage(content=f"user {i}", id=f"h{i}"))
        msgs.append(AIMessage(content=f"assistant {i}", id=f"a{i}"))
    return msgs


def _summary(result):
    for m in result["messages"]:
        if isinstance(m, HumanMessage) and (m.additional_kwargs or {}).get("lc_source") == "summarization":
            return m
    raise AssertionError("no summary message in result")


@pytest.fixture
def chat_model(monkeypatch):
    chat = _Recorder(reply="CHAT MODEL SUMMARY")

    def fake_get_llm(name):
        assert name == "muse-spark-1.2-contributor", name
        return chat

    monkeypatch.setattr(llm_factory, "get_llm", fake_get_llm)
    return chat


def test_summary_comes_from_the_model_in_use(chat_model):
    fallback = _Recorder(reply="FALLBACK SUMMARY")
    result = _mw(fallback).before_model(
        {"messages": _convo(), "llm_model": "muse-spark-1.2-contributor"}, None
    )
    assert "CHAT MODEL SUMMARY" in _summary(result).content
    assert chat_model.calls == 1
    assert fallback.calls == 0


def test_key_chain_is_the_fallback_when_the_chat_model_fails(chat_model):
    chat_model.reply = None  # raises
    fallback = _Recorder(reply="FALLBACK SUMMARY")
    result = _mw(fallback).before_model(
        {"messages": _convo(), "llm_model": "muse-spark-1.2-contributor"}, None
    )
    assert "FALLBACK SUMMARY" in _summary(result).content
    assert fallback.calls == 1


def test_get_llm_failing_falls_through_to_the_key_chain(monkeypatch):
    def boom(name):
        raise KeyError("no such model")

    monkeypatch.setattr(llm_factory, "get_llm", boom)
    fallback = _Recorder(reply="FALLBACK SUMMARY")
    result = _mw(fallback).before_model(
        {"messages": _convo(), "llm_model": "unknown-model"}, None
    )
    assert "FALLBACK SUMMARY" in _summary(result).content


def test_no_model_in_state_uses_the_key_chain(monkeypatch):
    monkeypatch.setattr(llm_factory, "get_llm", lambda name: pytest.fail("must not be asked"))
    fallback = _Recorder(reply="FALLBACK SUMMARY")
    result = _mw(fallback).before_model({"messages": _convo()}, None)
    assert "FALLBACK SUMMARY" in _summary(result).content


def test_every_summarizer_failing_never_stores_an_error_string(chat_model):
    chat_model.reply = None
    fallback = _Recorder(reply=None)
    result = _mw(fallback).before_model(
        {"messages": _convo(), "llm_model": "muse-spark-1.2-contributor"}, None
    )
    # The thread still compacts (a 600k thread is worse than a lossy one)...
    assert result is not None and isinstance(result["messages"][0], RemoveMessage)
    text = _summary(result).content
    # ...but the model is told the truth, not handed an exception as context.
    assert "Error generating summary" not in text
    assert "summarizer" in text.lower() or "unavailable" in text.lower()


@pytest.mark.asyncio
async def test_async_path_uses_the_model_in_use(chat_model):
    fallback = _Recorder(reply="FALLBACK SUMMARY")
    result = await _mw(fallback).abefore_model(
        {"messages": _convo(), "llm_model": "muse-spark-1.2-contributor"}, None
    )
    assert "CHAT MODEL SUMMARY" in _summary(result).content
    assert fallback.calls == 0


def test_raw_nodes_thread_their_model_through(chat_model, monkeypatch):
    fallback = _Recorder(reply="FALLBACK SUMMARY")
    mw = _mw(fallback)
    monkeypatch.setattr(summarization, "_compactor", lambda prompt, keep: mw)
    kept, ops = compact_messages_if_needed(
        _convo(), summary_prompt="Summarize:\n{messages}",
        llm_model="muse-spark-1.2-contributor",
    )
    assert ops, "expected a compaction"
    assert chat_model.calls == 1
    assert fallback.calls == 0
