"""Tests for token-usage extraction, including prompt-cache hit metadata.

These guard the regression where only input/output/total token counts were forwarded
to the mothership and the prompt-cache signal (input_token_details) was dropped, making
cache-hit rate impossible to measure downstream.
"""
from app.lib.token_usage import extract_token_usage


class _Msg:
    """Minimal stand-in for a LangChain AIMessage carrying usage_metadata."""

    def __init__(self, usage_metadata):
        self.usage_metadata = usage_metadata


def test_extract_includes_cache_read_for_deepseek_style():
    # DeepSeek (default model) surfaces cache hits via the OpenAI-compatible
    # prompt_tokens_details.cached_tokens, which LangChain maps to input_token_details.cache_read.
    msg = _Msg({
        "input_tokens": 4025,
        "output_tokens": 16,
        "total_tokens": 4041,
        "input_token_details": {"cache_read": 3968},
        "output_token_details": {"reasoning": 14},
    })
    usage = extract_token_usage(msg)
    assert usage["input_tokens"] == 4025
    assert usage["output_tokens"] == 16
    assert usage["total_tokens"] == 4041
    assert usage["cache_read_input_tokens"] == 3968
    assert usage["cache_creation_input_tokens"] == 0


def test_extract_includes_cache_creation_for_anthropic_style():
    # Anthropic/Claude reports both cache reads and cache writes (creation).
    msg = _Msg({
        "input_tokens": 5000,
        "output_tokens": 100,
        "total_tokens": 5100,
        "input_token_details": {"cache_read": 4000, "cache_creation": 800},
    })
    usage = extract_token_usage(msg)
    assert usage["cache_read_input_tokens"] == 4000
    assert usage["cache_creation_input_tokens"] == 800


def test_extract_handles_missing_cache_details():
    # A cold call (or a provider without caching) has no input_token_details.
    msg = _Msg({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    usage = extract_token_usage(msg)
    assert usage["input_tokens"] == 10
    assert usage["cache_read_input_tokens"] == 0
    assert usage["cache_creation_input_tokens"] == 0


def test_extract_returns_none_without_usage():
    assert extract_token_usage(_Msg(None)) is None
    assert extract_token_usage(_Msg({})) is None
