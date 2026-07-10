"""Shared LLM factory for all Leonardo agents.

Single source of truth for mapping a frontend `llm_model` string to a
configured LangChain chat model. Used by:

- DynamicModelMiddleware (in rails_agent/middleware.py) for agents built
  via `create_agent(...)` with middleware.
- Direct-invoke agents (rails_beginner_agent, rails_architect_agent,
  rails_ai_builder_agent, rails_frontend_starter_agent) that build their
  own StateGraph and call `llm.invoke(...)` inline.
- Sub-agent factories that spawn their own runtimes.

DO NOT duplicate this logic in agent nodes. Import `get_llm` instead.
"""

import logging
import os
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_deepseek import ChatDeepSeek
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen


logger = logging.getLogger(__name__)

DEFAULT_LLM_MODEL = "deepseek-v4-flash"


class FakeTestChatModel(BaseChatModel):
    """Deterministic offline model for e2e plumbing tests.

    Only reachable when LLAMABOT_ENABLE_FAKE_LLM=true and the frontend sends
    llm_model='fake-llm'. Returns a fixed response and never calls a tool,
    so agent turns complete immediately with zero API cost.
    """

    response_text: str = "FAKE_LLM_RESPONSE: end-to-end plumbing OK"

    @property
    def _llm_type(self) -> str:
        return "fake-llm"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response_text))]
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        # The websocket layer only renders streamed token chunks
        # (stream_mode="messages"), so the fake model must stream.
        for token in self.response_text.split(" "):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=token + " "))
            if run_manager:
                run_manager.on_llm_new_token(token + " ", chunk=chunk)
            yield chunk

    def bind_tools(self, tools, **kwargs):
        return self


class ChatDeepSeekWithReasoning(ChatDeepSeek):
    """ChatDeepSeek that preserves reasoning_content across multi-turn tool calls.

    DeepSeek's reasoner model requires reasoning_content to be present in
    assistant messages during multi-turn conversations with tool calls. The
    base ChatDeepSeek stores reasoning_content in additional_kwargs but drops
    it when sending messages back to the API.

    See: https://api-docs.deepseek.com/guides/thinking_mode#tool-calls
    Fix based on: https://github.com/langchain-ai/langchain/pull/34516
    """

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        reasoning_contents = {}
        messages_list = input_ if isinstance(input_, list) else [input_]

        for i, msg in enumerate(messages_list):
            if isinstance(msg, AIMessage):
                reasoning = msg.additional_kwargs.get("reasoning_content")
                reasoning_contents[i] = reasoning if reasoning is not None else ""

        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        assistant_idx = 0
        for message in payload["messages"]:
            if message["role"] == "assistant":
                original_idx = None
                count = 0
                for j, msg in enumerate(messages_list):
                    if isinstance(msg, AIMessage):
                        if count == assistant_idx:
                            original_idx = j
                            break
                        count += 1

                if original_idx is not None and original_idx in reasoning_contents:
                    message["reasoning_content"] = reasoning_contents[original_idx]
                elif "reasoning_content" not in message:
                    message["reasoning_content"] = ""

                assistant_idx += 1

        return payload


def get_llm(model_name: str):
    """Build a configured chat model for the given frontend model name.

    All models are configured with thinking/reasoning enabled where supported:
    - Gemini: include_thoughts=True
    - Claude: thinking with budget_tokens
    - OpenAI: reasoning with effort="low" + responses API
    - DeepSeek: reasoning_content preserved via ChatDeepSeekWithReasoning

    Unknown model names fall back to the project default (DeepSeek v4 Flash).

    Provider SDK retries are disabled here because they are opaque and multiply
    the 180-second request timeout. Middleware-based agents own classified,
    warning-logged retries; direct callers fail into the normal recovery path.
    """
    if model_name == "fake-llm" and os.getenv("LLAMABOT_ENABLE_FAKE_LLM", "").lower() == "true":
        return FakeTestChatModel()

    # Operator/mothership gate (see model_policy). This is the authoritative
    # chokepoint: the websocket llm_model is unvalidated user input, so a model
    # disabled by policy is swapped for an enabled one here, before any client is
    # built — the dropdown filtering in /api/available-models is only UX on top.
    # Deferred import avoids a circular import (model_policy reads DEFAULT_LLM_MODEL).
    from app.agents.leonardo.model_policy import enabled_default_model, is_model_enabled
    if not is_model_enabled(model_name):
        replacement = enabled_default_model()
        logger.warning(
            "Requested model %r is disabled by policy; using %r instead.",
            model_name, replacement,
        )
        model_name = replacement

    if model_name == "deepseek-v4-flash":
        return ChatDeepSeekWithReasoning(
            model="deepseek-v4-flash",
            timeout=180,
            max_retries=0,
        )
    if model_name == "deepseek-v4-pro":
        return ChatDeepSeekWithReasoning(
            model="deepseek-v4-pro",
            timeout=180,
            max_retries=0,
        )
    if model_name == "gpt-5-codex":
        return ChatOpenAI(
            model="gpt-5-codex",
            use_responses_api=True,
            reasoning={"effort": "low", "summary": "auto"},
            output_version="responses/v1",
            max_retries=0,
        )
    if model_name == "gpt-5-mini":
        return ChatOpenAI(
            model="gpt-5-mini",
            use_responses_api=True,
            reasoning={"effort": "low", "summary": "auto"},
            output_version="responses/v1",
            max_retries=0,
        )
    if model_name == "gpt-5-nano":
        return ChatOpenAI(
            model="gpt-5-nano",
            use_responses_api=True,
            reasoning={"effort": "low", "summary": "auto"},
            output_version="responses/v1",
            max_retries=0,
        )
    if model_name == "gpt-5.4-nano":
        return ChatOpenAI(
            model="gpt-5.4-nano",
            use_responses_api=True,
            reasoning={"effort": "low", "summary": "auto"},
            output_version="responses/v1",
            max_retries=0,
        )
    if model_name == "claude-4.5-sonnet":
        return ChatAnthropic(
            model="claude-sonnet-4-5-20250929",
            max_tokens=16384,
            thinking={"type": "enabled", "budget_tokens": 5000},
            max_retries=0,
        )
    if model_name == "claude-4.5-haiku":
        return ChatAnthropic(
            model="claude-haiku-4-5",
            max_tokens=16384,
            thinking={"type": "enabled", "budget_tokens": 3000},
            max_retries=0,
        )
    if model_name == "gemini-3-flash":
        return ChatGoogleGenerativeAI(
            model="gemini-3-flash-preview",
            include_thoughts=True,
            retries=0,
        )
    if model_name == "gemini-3-pro":
        return ChatGoogleGenerativeAI(
            model="gemini-3.1-pro-preview",
            include_thoughts=True,
            retries=0,
        )
    if model_name == "gemini-3.1-flash-lite":
        return ChatGoogleGenerativeAI(
            model="gemini-3.1-flash-lite",
            thinking_level="high",
            include_thoughts=True,
            retries=0,
        )
    if model_name == "qwen3.7-plus":
        # Alibaba Cloud Model Studio's Qwen3.7 Plus, via the dedicated
        # langchain-qwq ChatQwen client (handles thinking/reasoning_content
        # across multi-turn tool calls natively, unlike a bare ChatOpenAI).
        # qwen3.7-plus is the low-cost multimodal agent model (text/image/video,
        # 1M context) and is a hybrid thinking model. Defaults to the US
        # (Virginia) region; region keys are NOT interchangeable, so the
        # api_base must match the region the ALIBABA_API_KEY was issued in.
        return ChatQwen(
            model="qwen3.7-plus",
            api_base=os.getenv(
                "ALIBABA_BASE_URL",
                "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
            ),
            api_key=os.getenv("ALIBABA_API_KEY"),
            enable_thinking=True,
            thinking_budget=8192,
            max_retries=0,
        )

    return ChatDeepSeekWithReasoning(
        model="deepseek-v4-flash",
        timeout=180,
        max_retries=0,
    )


def make_summarization_model():
    """Build (model, token_counter, trim_tokens_to_summarize) for SummarizationMiddleware.

    Fallback chain, in priority order, picking the first provider whose API key is
    present in the environment:

        DeepSeek v4 Flash  ->  Gemini 3 Flash  ->  OpenAI gpt-5-mini  ->  Anthropic Haiku

    DeepSeek is first because it is the project default LLM (model policy), so the
    summarizer matches the conversation model and stays on the provider we
    actually pay for. Gemini is the strongest fallback (cheap, 1M multimodal
    context). OpenAI then Anthropic are last-resort so summarization keeps working
    on instances configured with only those keys.

    Graph compilation must NEVER raise here just because a key is missing
    (fleet-wide compile guard, SupportIncident #93) — every branch is gated on its
    own key, and the final fallback returns DeepSeek so a compiled graph always
    has *a* summarizer (it only errors at call time if literally no key exists).

    Returns a 3-tuple so the caller can configure the surrounding middleware:
    the token_counter and trim limit differ between the multimodal Gemini path
    (no trim — let it see everything) and the text-only providers (tiktoken
    counter + explicit trim guard to stay inside their context windows).
    """
    from app.agents.utils.token_counter import (
        gemini_multimodal_token_counter,
        tiktoken_token_counter,
    )

    # 1) DeepSeek — project default; text-only, tiktoken counter, explicit cap so
    #    the summarizer input stays inside DeepSeek's smaller context window.
    if os.getenv("DEEPSEEK_API_KEY"):
        return (
            ChatDeepSeekWithReasoning(model="deepseek-v4-flash", timeout=180),
            tiktoken_token_counter,
            60000,
        )

    # 2) Gemini 3 Flash — large multimodal context, can see the full conversation.
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return (
            ChatGoogleGenerativeAI(
                model="gemini-3-flash-preview",
                vertexai=False,
                temperature=1.0,
            ),
            gemini_multimodal_token_counter,
            None,
        )

    # 3) OpenAI — large context; tiktoken counts cleanly without an external call.
    if os.getenv("OPENAI_API_KEY"):
        return (
            ChatOpenAI(model="gpt-5-mini"),
            tiktoken_token_counter,
            None,
        )

    # 4) Anthropic — last resort.
    if os.getenv("ANTHROPIC_API_KEY"):
        return (
            ChatAnthropic(model="claude-haiku-4-5", max_tokens=8192),
            tiktoken_token_counter,
            None,
        )

    # Nothing configured: still return a (DeepSeek) model so graph compilation
    # succeeds (fleet-wide compile guard). ChatDeepSeek validates that an api_key
    # exists at *construction* time, so we pass a placeholder — the graph compiles
    # and only a summarization that actually fires would error, the same failure
    # surface as the chat model with no key.
    return (
        ChatDeepSeekWithReasoning(
            model="deepseek-v4-flash",
            timeout=180,
            api_key="missing-deepseek-api-key-placeholder",
        ),
        tiktoken_token_counter,
        60000,
    )
