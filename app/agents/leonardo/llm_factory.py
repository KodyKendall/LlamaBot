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


_MISSING_KEY_PLACEHOLDER = "missing-provider-api-key-placeholder"


def provider_key(*env_vars: str) -> str:
    """First configured key among `env_vars`, or a dud placeholder — never None.

    MUST be used for every model we point at a non-OpenAI `base_url`.

    All of these clients (ChatOpenAI, ChatDeepSeek, ChatQwen) sit on the openai
    SDK, and that SDK falls back to `OPENAI_API_KEY` from the environment when it
    is handed `api_key=None`. Since the base_url is a third party's, an instance
    that has OPENAI_API_KEY set but not the provider's own key would put our
    OpenAI secret in an `Authorization: Bearer` header addressed to GMI /
    Fireworks / Alibaba / Meta. Verified: the constructed client's auth_headers
    really do carry the OpenAI key.

    Returning a dud instead turns a silent credential disclosure into an honest
    401 from the provider. It must be a non-empty string, not None or "".
    """
    for env_var in env_vars:
        value = os.getenv(env_var)
        if value and value.strip():
            return value
    return _MISSING_KEY_PLACEHOLDER


# Models served by the signed-in user's ChatGPT plan (Codex backend) rather than
# by our OPENAI_API_KEY. Maps the frontend name -> the id OpenAI expects.
# See docs/dev/chatgpt_oauth_byo_subscription.md.
_CHATGPT_SUBSCRIPTION_MODELS = {
    "gpt-5.6-luna-chatgpt": "gpt-5.6-luna",
    "gpt-5.6-sol-chatgpt": "gpt-5.6-sol",
}


class ChatOpenAICodexBackend(ChatOpenAI):
    """ChatOpenAI for the ChatGPT-plan Codex backend, which forbids system messages.

    ``chatgpt.com/backend-api/codex`` rejects any ``role: "system"`` entry in the
    Responses API ``input`` array with::

        400 {"detail": "System messages are not allowed"}

    LangChain emits exactly that for a ``SystemMessage``. The backend instead
    expects the system prompt in the top-level ``instructions`` field (which is
    how OpenAI's own Codex client sends it), so hoist it there.

    Same shape of fix as ``ChatDeepSeekWithReasoning`` above: a provider quirk
    handled once in the client rather than by asking every agent to build its
    messages differently. Note this is a DIFFERENT quirk from the one
    ``system_message_for_model`` solves (Fireworks/GMI rejecting Anthropic-style
    system *block lists*) — that one flattens a list into a string and still
    sends a system role, which this endpoint would still refuse.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # The backend refuses server-side response storage:
        #   400 {"detail": "Store must be set to false"}
        # Pinned here rather than passed at construction so it cannot be
        # overridden per-call. It is also the retention posture we want — the
        # same reason the Fireworks entry above stays on chat completions.
        payload["store"] = False

        messages = payload.get("input")
        if not isinstance(messages, list):
            return payload

        system_texts, kept = [], []
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "system":
                system_texts.append(_flatten_text(message.get("content")))
            else:
                kept.append(message)

        if not system_texts:
            return payload

        existing = payload.get("instructions")
        combined = [t for t in system_texts if t]
        if existing:
            combined.append(existing)

        payload["input"] = kept
        payload["instructions"] = "\n\n".join(combined)
        return payload


def _flatten_text(content) -> str:
    """Best-effort text out of a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _chatgpt_subscription_client(model_name: str):
    """Build a client bound to the current user's ChatGPT credential, or None.

    None means "no usable credential" — the caller must fall open to the default
    model. This function never raises: an unreachable auth DB, an expired refresh
    token or a revoked grant all read as None.
    """
    try:
        from app.lib.request_context import current_user_id
        from app.services.chatgpt_auth import (
            CODEX_BASE_URL,
            ORIGINATOR,
            access_token_for_user_sync,
        )

        user_id = current_user_id()
        if user_id is None:
            # Fails closed by design — never guess whose subscription to spend.
            logger.warning(
                "ChatGPT-subscription model %r requested with no user in context.",
                model_name,
            )
            return None

        creds = access_token_for_user_sync(user_id)
        if not creds or not creds[0]:
            return None
        access_token, account_id = creds

        headers = {"originator": ORIGINATOR}
        if account_id:
            headers["chatgpt-account-id"] = account_id

        return ChatOpenAICodexBackend(
            model=_CHATGPT_SUBSCRIPTION_MODELS[model_name],
            base_url=CODEX_BASE_URL,
            # An OAuth access token, not an API key. Passed explicitly and never
            # None — see provider_key's docstring: a None here would send our
            # OPENAI_API_KEY to chatgpt.com.
            api_key=access_token,
            use_responses_api=True,
            reasoning={"effort": "low", "summary": "auto"},
            output_version="responses/v1",
            default_headers=headers,
            timeout=180,
            max_retries=0,
        )
    except Exception as e:
        logger.warning("Could not build ChatGPT-subscription client for %r: %s", model_name, e)
        return None


def supports_prompt_caching(model_name: str) -> bool:
    """Whether this model accepts Anthropic's ephemeral prompt-caching kwarg.

    Single source of truth — do not re-derive with a literal `startswith("claude")`
    at a call site. `cache_control=` is Anthropic-only; passing it to an
    OpenAI-compatible client (deepseek, gpt, most of the fleet) raises
    `TypeError: Completions.create() got an unexpected keyword argument
    'cache_control'` and 500s every agent turn on that box.
    """
    return (model_name or "").startswith(("claude", "anthropic"))


def system_message_for_model(system_message, model_name: str):
    """Return a system message the selected provider will actually accept.

    Every Leonardo agent builds its system prompt in Anthropic's prompt-caching
    shape — a LIST of content blocks carrying `cache_control`. Anthropic needs
    that list; DeepSeek's own API tolerates it. The OpenAI-compatible gateways we
    serve `deepseek-v4-flash` through — **Fireworks and GMI** — do NOT: they
    strictly require system `content` to be a plain string and 400 the turn
    otherwise. Since the fleet default is policy-remapped to
    `deepseek-v4-flash-fireworks`, that 400 hit every mode (Database Mode first).

    So: for non-Anthropic models the blocks are flattened to their concatenated
    text; for Anthropic the message is returned untouched, because flattening it
    would silently drop prompt caching (~90% of input token cost).

    Accepts a `SystemMessage`, a raw ``{"role": "system", ...}`` dict (what the
    raw StateGraph agents build), a plain string, or None — and returns the same
    shape. `supports_prompt_caching` remains the single source of truth for the
    provider test; do not re-derive it with a literal `startswith("claude")`.
    """
    if system_message is None or supports_prompt_caching(model_name):
        return system_message

    if isinstance(system_message, dict):
        content = system_message.get("content")
        flattened = _flatten_content_blocks(content)
        if flattened is content:
            return system_message
        return {**system_message, "content": flattened}

    content = getattr(system_message, "content", None)
    flattened = _flatten_content_blocks(content)
    if flattened is content:
        return system_message
    return system_message.model_copy(update={"content": flattened})


def _flatten_content_blocks(content):
    """Join a list of text content blocks into one string; pass anything else through."""
    if not isinstance(content, list):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n\n".join(p for p in parts if p)


def invoke_with_cache(runnable, messages, model_name: str):
    """Invoke `runnable`, adding Anthropic ephemeral caching only where supported.

    Prefer attaching `cache_control` to a SystemMessage *content block* (see
    `rails_agent/nodes.py:get_sys_msg`) — non-Anthropic providers ignore that
    harmlessly. Use this helper when the kwarg form is genuinely needed.
    """
    if supports_prompt_caching(model_name):
        return runnable.invoke(messages, cache_control={"type": "ephemeral"})
    return runnable.invoke(messages)


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
    if model_name == "deepseek-v4-flash-gmi":
        # DeepSeek V4 Flash served by GMI Cloud instead of DeepSeek's own API.
        # Deliberately a SEPARATE model entry from `deepseek-v4-flash` (which
        # keeps pointing at api.deepseek.com) so the provider is an explicit
        # user choice, not a hidden swap.
        #
        # GMI is OpenAI-compatible and returns DeepSeek's reasoning_content
        # (streamed in deltas too), so the same ChatDeepSeekWithReasoning client
        # works — only the endpoint, key and model id differ. GMI is also more
        # lenient than DeepSeek direct: it accepts assistant messages with no
        # reasoning_content, so DeepSeekReasoningMiddleware (which gates on the
        # exact name "deepseek-v4-flash" and therefore does NOT fire here) is
        # not needed for this path.
        return ChatDeepSeekWithReasoning(
            model=os.getenv("GMI_DEEPSEEK_MODEL", "deepseek-ai/DeepSeek-V4-Flash"),
            api_base=os.getenv("GMI_BASE_URL", "https://api.gmi-serving.com/v1"),
            api_key=provider_key("GMI_DEEPSEEK_API_KEY"),
            timeout=180,
            max_retries=0,
        )
    if model_name == "deepseek-v4-flash-fireworks":
        # DeepSeek V4 Flash served by Fireworks AI. Like the GMI entry, a
        # SEPARATE model from `deepseek-v4-flash` so the provider is an explicit
        # choice rather than a hidden swap.
        #
        # Chosen over DeepSeek direct for data-retention reasons (US-hosted open
        # weights, so no Chinese data jurisdiction) and over GMI on prompt-cache
        # behavior: measured ~90% token-weighted prefix-cache hit rate (27/30
        # calls, misses are all-or-nothing) vs ~20% on GMI's shared fleet. Prompt
        # caching is per-replica, so a provider's routing — not the model —
        # decides the hit rate.
        #
        # Fireworks is OpenAI-compatible, separates reasoning_content (streamed
        # in deltas), reports cache hits via usage.prompt_tokens_details, and
        # accepts assistant messages without reasoning_content — so, as with GMI,
        # DeepSeekReasoningMiddleware (gated on the exact name
        # "deepseek-v4-flash") does not need to fire for this path.
        #
        # NOTE: stay on chat completions. Fireworks' *Response* API defaults to
        # store=True with 30-day retention, which would defeat the retention
        # rationale above.
        return ChatDeepSeekWithReasoning(
            model=os.getenv(
                "FIREWORKS_DEEPSEEK_MODEL",
                "accounts/fireworks/models/deepseek-v4-flash",
            ),
            api_base=os.getenv(
                "FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1"
            ),
            api_key=provider_key("FIREWORKS_DEEPSEEK_API_KEY"),
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
    if model_name == "gpt-5.6-luna":
        # `gpt-5.6-luna`, NOT the bare `gpt-5.6` alias — that alias routes to Sol,
        # a different (and much pricier) tier of the same family. Luna is the
        # cost/latency tier, roughly where nano sat in the GPT-5 family.
        return ChatOpenAI(
            model="gpt-5.6-luna",
            use_responses_api=True,
            reasoning={"effort": "low", "summary": "auto"},
            output_version="responses/v1",
            max_retries=0,
        )
    if model_name in _CHATGPT_SUBSCRIPTION_MODELS:
        # Runs on the SIGNED-IN USER's ChatGPT plan, not our OPENAI_API_KEY.
        #
        # Same model ids as the pay-per-token entries above (`gpt-5.6-luna`), but
        # a different payer and a different endpoint — the ChatGPT Codex backend
        # rather than api.openai.com. They are deliberately separate dropdown
        # entries so it is always visible which credential a turn is spending.
        #
        # Falls open to the operator default whenever the user has not connected
        # an account, the token cannot be refreshed, or OpenAI has revoked it.
        # A dead subscription must degrade to DeepSeek, never break the turn.
        client = _chatgpt_subscription_client(model_name)
        if client is not None:
            return client
        fallback = enabled_default_model()
        logger.warning(
            "No usable ChatGPT credential for %r; falling back to %r. "
            "The user needs to connect their account under Settings.",
            model_name, fallback,
        )
        if fallback not in _CHATGPT_SUBSCRIPTION_MODELS:
            return get_llm(fallback)
        return ChatDeepSeekWithReasoning(
            model="deepseek-v4-flash", timeout=180, max_retries=0
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
            api_key=provider_key("ALIBABA_API_KEY"),
            enable_thinking=True,
            thinking_budget=8192,
            max_retries=0,
        )
    if model_name == "muse-spark-1.2-contributor":
        # Meta's Muse Spark 1.2 (Meta Superintelligence Labs), CONTRIBUTOR tier.
        #
        # The Model API is OpenAI-compatible chat completions, so a plain
        # ChatOpenAI with an overridden base_url is the whole client — no new
        # dependency. The base_url is load-bearing: without it ChatOpenAI talks
        # to api.openai.com, which has never heard of this model id.
        #
        # TIER WARNING — the tier is encoded ONLY in the model id, and the two
        # ids differ by ~12x in price and completely in data handling:
        #   muse-spark-1.2              $1.25/$4.25 per 1M, not trained on
        #   muse-spark-1.2-contributor  $0.10/$0.20 per 1M, Meta trains on every
        #                               prompt and completion we send it
        # This entry is deliberately the contributor tier (explicit product
        # decision). Note that is the opposite trade from
        # `deepseek-v4-flash-fireworks`, which exists specifically to keep
        # customer code out of a third party's hands — so this model should not
        # be made a fleet default without revisiting that. An operator can move a
        # single instance to the paid, non-training tier with
        # META_MUSE_MODEL=muse-spark-1.2 without a code change.
        #
        # Meta's own docs name the key MODEL_API_KEY while their LiteLLM
        # integration uses META_API_KEY; we accept either, preferring META_API_KEY.
        #
        # `provider_key` (not os.getenv) is load-bearing here — see its docstring:
        # a None key would send our OpenAI secret to api.meta.ai.
        return ChatOpenAI(
            model=os.getenv("META_MUSE_MODEL", "muse-spark-1.2-contributor"),
            base_url=os.getenv("META_BASE_URL", "https://api.meta.ai/v1"),
            api_key=provider_key("META_API_KEY", "MODEL_API_KEY"),
            reasoning_effort="low",
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
