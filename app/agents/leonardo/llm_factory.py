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
    """
    if model_name == "fake-llm" and os.getenv("LLAMABOT_ENABLE_FAKE_LLM", "").lower() == "true":
        return FakeTestChatModel()
    if model_name == "deepseek-v4-flash":
        return ChatDeepSeekWithReasoning(
            model="deepseek-v4-flash",
            timeout=180,
        )
    if model_name == "deepseek-v4-pro":
        return ChatDeepSeekWithReasoning(
            model="deepseek-v4-pro",
            timeout=180,
        )
    if model_name == "gpt-5-codex":
        return ChatOpenAI(
            model="gpt-5-codex",
            use_responses_api=True,
            reasoning={"effort": "low", "summary": "auto"},
            output_version="responses/v1",
        )
    if model_name == "gpt-5-mini":
        return ChatOpenAI(
            model="gpt-5-mini",
            use_responses_api=True,
            reasoning={"effort": "low", "summary": "auto"},
            output_version="responses/v1",
        )
    if model_name == "claude-4.5-sonnet":
        return ChatAnthropic(
            model="claude-sonnet-4-5-20250929",
            max_tokens=16384,
            thinking={"type": "enabled", "budget_tokens": 5000},
        )
    if model_name == "claude-4.5-haiku":
        return ChatAnthropic(
            model="claude-haiku-4-5",
            max_tokens=16384,
            thinking={"type": "enabled", "budget_tokens": 3000},
        )
    if model_name == "gemini-3-flash":
        return ChatGoogleGenerativeAI(
            model="gemini-3-flash-preview",
            include_thoughts=True,
        )
    if model_name == "gemini-3-pro":
        return ChatGoogleGenerativeAI(
            model="gemini-3.1-pro-preview",
            include_thoughts=True,
        )
    if model_name == "gemini-3.1-flash-lite":
        return ChatGoogleGenerativeAI(
            model="gemini-3.1-flash-lite",
            thinking_level="high",
            include_thoughts=True,
        )
    if model_name == "qwen3-vl-plus":
        # Alibaba Cloud Model Studio's Qwen VL, via the dedicated langchain-qwq
        # ChatQwen client (handles thinking/reasoning_content across multi-turn
        # tool calls natively, unlike a bare ChatOpenAI). qwen3-vl-plus is the
        # full/most-capable vision model and is a hybrid thinking model. Defaults
        # to the US (Virginia) region; region keys are NOT interchangeable, so
        # the api_base must match the region the ALIBABA_API_KEY was issued in.
        return ChatQwen(
            model="qwen3-vl-plus",
            api_base=os.getenv(
                "ALIBABA_BASE_URL",
                "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
            ),
            api_key=os.getenv("ALIBABA_API_KEY"),
            enable_thinking=True,
            thinking_budget=8192,
        )

    return ChatDeepSeekWithReasoning(
        model="deepseek-v4-flash",
        timeout=180,
    )
