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

from app.agents.leonardo.openrouter_models import (
    api_base as openrouter_api_base,
    get_openrouter_model,
)


logger = logging.getLogger(__name__)

# The fleet default (0.7.0). Note this is the CONTRIBUTOR tier — see the tier
# warning on the `muse-spark-1.2-contributor` branch in `get_llm` before moving
# any box onto it; an operator pins the paid, non-training tier per box with
# META_MUSE_MODEL=muse-spark-1.2.
DEFAULT_LLM_MODEL = "muse-spark-1.2-contributor"

# What the default degrades to on a box with no usable META key. `get_llm` can
# always build this one, so it is what keeps chat working on a box the Muse
# rollout has not reached (or that deliberately opts out).
FALLBACK_TEXT_MODEL = "deepseek-v4-flash"

# --- Stall detection -------------------------------------------------------
#
# How long an async stream may go without producing a content chunk before the
# client gives up on it. This is `langchain_openai`'s `stream_chunk_timeout`,
# which measures the gap between *parsed* chunks — SSE keepalives do not reset
# it, so it is the only thing that catches "HTTP 200, then nothing".
#
# 25s is chosen, not inherited. The library's own default is 120s, and on
# 2026-08-26 a Muse Spark endpoint accepted requests and streamed nothing:
# 120s per attempt x 5 rung-1 attempts was ~10 minutes of a spinning shimmer
# before anything else in the ladder got a turn. No healthy call on any fleet
# model waits 25s for a first chunk (measured 0.5-2.0s on the same endpoint an
# hour later), so this only ever fires on a genuinely dead stream.
#
# Deliberately NOT disabled and not unbounded — a disabled chunk timeout is how
# you get a turn that hangs forever. The env var is the same one the library
# reads, so an operator override lands once and means one thing.
STREAM_CHUNK_TIMEOUT_ENV = "LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S"
_DEFAULT_STREAM_CHUNK_TIMEOUT_S = 25.0


def stream_chunk_timeout_s() -> float:
    """Seconds of content silence tolerated on a stream before it is abandoned."""
    raw = os.getenv(STREAM_CHUNK_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
            logger.warning(
                "%s=%r is not positive; a disabled chunk timeout hangs a turn "
                "forever. Using %ss.", STREAM_CHUNK_TIMEOUT_ENV, raw,
                _DEFAULT_STREAM_CHUNK_TIMEOUT_S,
            )
        except ValueError:
            logger.warning(
                "%s=%r is not a number; using %ss.",
                STREAM_CHUNK_TIMEOUT_ENV, raw, _DEFAULT_STREAM_CHUNK_TIMEOUT_S,
            )
    return _DEFAULT_STREAM_CHUNK_TIMEOUT_S


def _apply_stream_chunk_timeout(client):
    """Stamp the chosen chunk timeout onto a freshly built client.

    Applied here rather than in each constructor call because llm_factory has
    eleven `ChatOpenAI(` call sites, each with its own kwargs dict; a per-site
    fix reliably misses one and leaves a 120s hole in the fleet. Set after
    construction so the branches stay untouched.

    Silently skipped for clients that have no such field (Anthropic, Gemini —
    they are not OpenAI-compatible and carry their own timeouts), and never
    fatal: a client that cannot take the hint is still a working client.
    """
    try:
        if "stream_chunk_timeout" in getattr(type(client), "model_fields", {}):
            client.stream_chunk_timeout = stream_chunk_timeout_s()
    except Exception as e:  # noqa: BLE001 - never break model construction
        logger.debug("could not set stream_chunk_timeout on %r: %s", type(client), e)
    return client


# DeepSeek's own API (api.deepseek.com), as opposed to the same weights served by
# GMI/Fireworks. Only these need DeepSeekReasoningMiddleware: DeepSeek direct is
# the strict one about assistant messages carrying reasoning_content, while the
# OpenAI-compatible resellers accept messages without it. Kept here rather than
# in the middleware so adding a DeepSeek-direct model is one edit, not two —
# `deepseek-v4-pro` spent several releases missing from the middleware's
# hardcoded name check precisely because they were separate lists.
DEEPSEEK_DIRECT_MODELS = frozenset({
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-vision-exp",
})


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


# Which env vars credential the models a POLICY DEFAULT can resolve to, in the
# same precedence `get_llm` uses to build them. Deliberately not the full
# registry — this map answers "can this box actually construct the model it is
# about to fall back to", not "what is in the dropdown", which belongs to
# /api/available-models (test_model_registry_consistency pins them in agreement).
#
# Two kinds of default resolve through here: the default TEXT model
# (`default_text_model`) and the box's vision model (`vision_model`), which is
# why the vision entry is listed alongside the other two.
DEFAULT_MODEL_KEY_ENVS = {
    "muse-spark-1.2-contributor": ("META_API_KEY", "MODEL_API_KEY"),
    "deepseek-v4-flash": ("DEEPSEEK_API_KEY",),
    "deepseek-v4-flash-vision-exp": ("DEEPSEEK_API_KEY",),
}


def has_provider_key(model_name: str) -> bool:
    """True if this box holds a credential for `model_name`.

    Used by the model policy to keep the resolved default to something `get_llm`
    can build — a default naming a keyless model is not a degraded box, it is a
    box where every turn 401s.

    A model absent from `DEFAULT_MODEL_KEY_ENVS` reports True: this is not a
    general reachability check and must not start disabling models it has no
    opinion about.
    """
    env_vars = DEFAULT_MODEL_KEY_ENVS.get(model_name)
    if not env_vars:
        return True
    return provider_key(*env_vars) != _MISSING_KEY_PLACEHOLDER


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
    from app.agents.leonardo.model_policy import effective_model, enabled_default_model
    replacement = effective_model(model_name)
    if replacement != model_name:
        logger.warning(
            "Requested model %r is disabled by policy; using %r instead.",
            model_name, replacement,
        )
        model_name = replacement

    # Construction lives in _build_client so the stall guard is applied in ONE
    # place. See _apply_stream_chunk_timeout: the alternative was editing eleven
    # constructor call sites and missing one.
    return _apply_stream_chunk_timeout(_build_client(model_name))


def _build_client(model_name: str):
    """Construct the provider client for an ALREADY policy-resolved model name.

    Split out of :func:`get_llm` purely so every branch below flows through one
    post-construction hook. Call ``get_llm``, never this — the operator policy
    gate lives up there, and this function will happily build a model the box is
    not allowed to run.
    """
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
    # Config-driven OpenRouter entries (see openrouter_models). ONE branch serves
    # every OpenRouter endpoint — the provider pin, model id and client choice all
    # come from the registry — so adding an endpoint is a config block, not a code
    # change. Checked BEFORE the hardcoded branches so an overlay can deliberately
    # shadow a compiled-in name; checked AFTER the policy gate above so a
    # registered model is still subject to DISABLED_MODELS like any other.
    openrouter_entry = get_openrouter_model(model_name)
    if openrouter_entry is not None:
        # provider_key(), never a bare os.getenv: the OpenAI SDK falls back to
        # OPENAI_API_KEY when api_key is None, which would send our OpenAI key to
        # openrouter.ai (see test_provider_key_never_leaks_openai).
        kwargs = dict(
            model=openrouter_entry["model"],
            api_base=openrouter_api_base(),
            api_key=provider_key("OPENROUTER_API_KEY"),
            timeout=180,
            max_retries=0,
        )
        extra_body = openrouter_entry.get("extra_body")
        if extra_body:
            # Where the provider/quantization pin actually travels. OpenRouter
            # reads `provider` as a top-level request field, and extra_body is
            # what the OpenAI-compatible client passes through untouched.
            kwargs["extra_body"] = extra_body
        if openrouter_entry.get("reasoning", True):
            return ChatDeepSeekWithReasoning(**kwargs)
        return ChatOpenAI(**kwargs)

    if model_name == "deepseek-v4-flash-vision-exp":
        # DeepSeek V4 Flash's multimodal sibling on DeepSeek's own API — the only
        # vision model a box can run on the DEEPSEEK_API_KEY it already has.
        # Before this entry, image understanding anywhere in the fleet required a
        # META key for Muse, so a DeepSeek-only box refused image attachments
        # outright (see model_policy.vision_model).
        #
        # Verified live against api.deepseek.com (2026-08-25), because the vision
        # guide documents none of it: image + tool calling in one request works
        # (returns a real tool_calls block describing the image), tool calling
        # without an image works, a system prompt alongside an image works, and
        # reasoning_content still streams as deltas — so the same
        # ChatDeepSeekWithReasoning client carries it, only the model id differs.
        #
        # Two API-side constraints worth knowing (both 400s, both verified):
        #   * images are accepted ONLY in user messages — an image block in an
        #     assistant message is rejected outright. Nothing sends one today;
        #     don't add an image-carrying tool result without re-checking.
        #   * the text-only models reject images ("This model does not support
        #     image"), which is what MODEL_CAPABILITIES + the multimodal stripper
        #     exist to prevent.
        #
        # `-exp` is DeepSeek's own name for it: experimental, with no published
        # deprecation policy. Treat a sudden 400 on this id as "it was withdrawn"
        # rather than a bug on our side — vision_model() falls back on its own.
        return ChatDeepSeekWithReasoning(
            model="deepseek-v4-flash-vision-exp",
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
                # Fireworks retired the unversioned `deepseek-v4-flash` alias
                # (2026-08: 404 "Model not found, inaccessible, and/or not
                # deployed"); only dated snapshots are listed now. Pin the
                # snapshot explicitly and re-pin when Fireworks publishes a
                # newer one — the env var is the escape hatch in between.
                "FIREWORKS_DEEPSEEK_MODEL",
                "accounts/fireworks/models/deepseek-v4-flash-0731",
            ),
            api_base=os.getenv(
                "FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1"
            ),
            api_key=provider_key("FIREWORKS_DEEPSEEK_API_KEY"),
            timeout=180,
            max_retries=0,
        )
    if model_name == "nemotron-lightning-30b-fireworks":
        # NVIDIA Nemotron 3.5 Lightning 30B-A3B on Fireworks' SERVERLESS tier —
        # the same weights as `nemotron-lightning-30b-runpod`, with nobody having
        # to keep a pod alive. A SIBLING entry, not a re-point of the RunPod one:
        # who serves (and bills for) a turn stays an explicit user choice, and the
        # self-hosted entry stays available for the boxes that have a pod.
        #
        # Client is ChatDeepSeekWithReasoning rather than a bare ChatOpenAI, and
        # that is verified against the live endpoint (2026-08-16): Fireworks
        # returns Nemotron's thinking in a separate `reasoning_content` field,
        # streams it as deltas, and accepts assistant messages that carry it back
        # — exactly the shape that client exists for, same as the Fireworks
        # DeepSeek entry above. A plain ChatOpenAI drops the thinking on the floor.
        #
        # DO NOT add a max_tokens cap, same reason as the RunPod entry: reasoning
        # bills against max_tokens while being stripped from the response, so a
        # small cap returns EMPTY content with no error at all. Fireworks imposes
        # no small default of its own (verified: an uncapped request ran to 2705
        # completion tokens and finished with `stop`), so leaving it unset is safe.
        #
        # Key: FIREWORKS_API_KEY is the account-wide name from Fireworks' own
        # docs; FIREWORKS_DEEPSEEK_API_KEY is accepted as a fallback because it is
        # the name already deployed on boxes running the DeepSeek entry. One
        # Fireworks account issues one key, so a per-model key name would be
        # fiction — this is the same "accept either" precedent as Meta's
        # META_API_KEY / MODEL_API_KEY. /api/available-models checks both, in this
        # same order.
        return ChatDeepSeekWithReasoning(
            model=os.getenv(
                "FIREWORKS_NEMOTRON_MODEL",
                "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
            ),
            api_base=os.getenv(
                "FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1"
            ),
            api_key=provider_key("FIREWORKS_API_KEY", "FIREWORKS_DEEPSEEK_API_KEY"),
            timeout=180,
            max_retries=0,
        )
    if model_name == "qwen3-8b-runpod":
        # Qwen3-8B served by vLLM on our own RunPod GPU. Same house pattern as
        # the GMI/Fireworks entries: a SEPARATE, explicitly-named model rather
        # than a hidden re-point of an existing one.
        #
        # vLLM speaks OpenAI-compatible chat completions, so a plain ChatOpenAI
        # with an overridden base_url is the whole client (same precedent as the
        # Meta branch below). The base_url has NO default on purpose: the pod URL
        # is sensitive and rotatable, so it is env-only and never committed — a
        # box without RUNPOD_QWEN_BASE_URL simply shows the model greyed out
        # (/api/available-models keys this model's availability on that var).
        #
        # Enabling it on a box takes both RUNPOD_QWEN_BASE_URL *and* naming the
        # model in ENABLED_MODELS — model_policy's default allow-list is the
        # compiled two-model set, not "anything that happens to be configured".
        #
        # `provider_key` (not os.getenv) is load-bearing even though the pod is
        # unauthenticated today — see its docstring: api_key=None would let the
        # openai SDK fall back to OPENAI_API_KEY and address our OpenAI secret to
        # a RunPod proxy URL. It also means a future `vllm serve --api-key` needs
        # only RUNPOD_QWEN_API_KEY in .env, no code change.
        #
        # Thinking is disabled client-side: Qwen3 otherwise emits
        # <think>...</think> inside `content`, which pollutes tool-calling turns.
        # The alternative is pod-side (`--reasoning-parser qwen3`, which splits it
        # into reasoning_content) — a serve-command change, so not this PR's call.
        return ChatOpenAI(
            model=os.getenv("RUNPOD_QWEN_MODEL", "Qwen/Qwen3-8B"),
            base_url=os.getenv("RUNPOD_QWEN_BASE_URL"),
            api_key=provider_key("RUNPOD_QWEN_API_KEY"),
            timeout=180,
            max_retries=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    if model_name == "muse-glimmer-30b-runpod":
        # Muse Glimmer 30B (community AWQ INT4 checkpoint) on the same
        # self-hosted vLLM pod pattern as `qwen3-8b-runpod` above: env-only
        # base_url, provider-explicit name, greyed out on a box without it.
        #
        # A SIBLING entry, not a re-point of the qwen one — deliberately its own
        # RUNPOD_GLIMMER_* triple. One env triple per model identity: the pods
        # are genuinely separate servers (the Nemotron entry below runs on a
        # third pod concurrently), and aiming RUNPOD_QWEN_MODEL at a Meta
        # checkpoint is exactly the hidden swap this house style forbids.
        #
        # NO `chat_template_kwargs`/`enable_thinking` here, unlike the qwen
        # branch: that kwarg is a Qwen chat-template feature, and Glimmer's
        # reasoning is already separated server-side by vLLM's
        # `--reasoning-parser muse_glimmer`, so `content` arrives clean. Passing
        # an unknown kwarg into a template that never declared it risks a 400 for
        # no benefit.
        return ChatOpenAI(
            model=os.getenv(
                "RUNPOD_GLIMMER_MODEL", "cyankiwi/Muse-Glimmer-30B-AWQ-INT4"
            ),
            base_url=os.getenv("RUNPOD_GLIMMER_BASE_URL"),
            api_key=provider_key("RUNPOD_GLIMMER_API_KEY"),
            timeout=180,
            max_retries=0,
        )
    if model_name == "nemotron-lightning-30b-runpod":
        # NVIDIA Nemotron 3.5 Lightning 30B-A3B (official NVFP4 checkpoint) on
        # its OWN RunPod pod (RTX 5090) — a third pod running concurrently with
        # the Glimmer one, hence a third independent env triple.
        #
        # Served with `--max-model-len 131072`, ~4x the other pod, which is what
        # makes it the first self-hosted entry with real headroom over Leo's
        # ~27k engineer-prompt floor.
        #
        # No `chat_template_kwargs` here, same as the Glimmer branch: reasoning
        # is separated server-side by `--reasoning-parser nemotron_v3` and tool
        # calls by `--tool-call-parser qwen3_coder`, so `content` arrives clean.
        #
        # DO NOT add a max_tokens cap to this path. The model spends ~250
        # reasoning tokens even on trivial prompts, and that reasoning bills
        # against max_tokens while being stripped from the response — so a small
        # cap returns EMPTY content with no error at all (seen live at 500).
        return ChatOpenAI(
            model=os.getenv(
                "RUNPOD_NEMOTRON_MODEL",
                "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4",
            ),
            base_url=os.getenv("RUNPOD_NEMOTRON_BASE_URL"),
            api_key=provider_key("RUNPOD_NEMOTRON_API_KEY"),
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
        # Deferred, like the policy import in get_llm, for the same circular-
        # import reason (model_policy reads DEFAULT_LLM_MODEL from this module).
        from app.agents.leonardo.model_policy import enabled_default_model

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
    if model_name == "qwen3.8-27b-hetzner":
        # Qwen3.8-27B on Hetzner's Inference API — an EU-hosted,
        # OpenAI-compatible endpoint, so a plain ChatOpenAI with an overridden
        # base_url is the whole client (same shape as the self-hosted vLLM
        # entries above). The base_url is load-bearing: without it ChatOpenAI
        # talks to api.openai.com, which has never heard of this model id.
        #
        # Deliberately NOT ChatQwen: langchain_qwq exists for Alibaba
        # DashScope's thinking/reasoning_content shape (see `qwen3.7-plus`).
        # This is a generic gateway in front of open weights, not DashScope.
        #
        # Dense 27B, 262k context, text + image in. Hetzner's /v1/models is the
        # definitive list of what they serve; HETZNER_QWEN_MODEL moves this
        # entry to their other one (`Qwen/Qwen3.6-35B-A3B-FP8`, MoE, 35B total /
        # 3B active, same context and modalities) with no code change.
        #
        # THE SHARP EDGE IS REQUESTS, NOT TOKENS: 10 requests per 60s per key
        # (alongside a generous 4M in / 100k out tokens per 60s). A single
        # agentic Leo turn is many sequential requests, so even one busy user on
        # a key will hit 429s. Those are classified transient and retried by the
        # resilience middleware, so they show up as slow turns rather than
        # failed ones — but it is why this must not become a fleet default while
        # the API is in its free experimental phase.
        #
        # `provider_key` (not os.getenv) is load-bearing — see its docstring: an
        # api_key of None would address our OpenAI secret to inference.hetzner.com.
        #
        # No `chat_template_kwargs={"enable_thinking": False}` here, unlike the
        # RunPod Qwen3-8B branch: we control neither Hetzner's serve flags nor
        # their chat template, and an unknown kwarg risks a 400 for no benefit.
        # If thinking leaks inline as <think>...</think> inside `content`, that
        # is the first knob to try.
        return ChatOpenAI(
            model=os.getenv("HETZNER_QWEN_MODEL", "Qwen3.8-27B"),
            base_url=os.getenv(
                "HETZNER_BASE_URL", "https://inference.hetzner.com/api/v1"
            ),
            api_key=provider_key("HETZNER_API_KEY"),
            timeout=180,
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
