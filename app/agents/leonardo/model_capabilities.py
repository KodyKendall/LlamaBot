"""
Single source of truth for per-model multimodal capabilities.

Both the websocket request handler (which decides what to attach to a *new*
outgoing message) and the rails-agent middleware (which scrubs unsupported
content out of *replayed history* before each LLM call) read from here, so the
capability table never drifts between the two.
"""

# Each model has different support for images, video, PDFs, etc.
MODEL_CAPABILITIES = {
    # Gemini models support video, images, PDFs
    'gemini-3-flash': {'images': True, 'video': True, 'pdf': True},
    'gemini-3-pro': {'images': True, 'video': True, 'pdf': True},
    'gemini-3.1-flash-lite': {'images': True, 'video': True, 'pdf': True},
    'gemini-2.5-flash': {'images': True, 'video': True, 'pdf': True},
    'gemini-2.5-pro': {'images': True, 'video': True, 'pdf': True},

    # Claude models support images and PDFs only (no video)
    'claude-4.5-haiku': {'images': True, 'video': False, 'pdf': True},
    'claude-4.5-sonnet': {'images': True, 'video': False, 'pdf': True},
    'claude-sonnet-4': {'images': True, 'video': False, 'pdf': True},
    'claude-opus-4': {'images': True, 'video': False, 'pdf': True},

    # OpenAI/GPT models support images only
    'gpt-4o': {'images': True, 'video': False, 'pdf': False},
    'gpt-4o-mini': {'images': True, 'video': False, 'pdf': False},
    'gpt-5-codex': {'images': True, 'video': False, 'pdf': False},
    'gpt-5-mini': {'images': True, 'video': False, 'pdf': False},
    'gpt-5-nano': {'images': True, 'video': False, 'pdf': False},
    'gpt-5.4-nano': {'images': True, 'video': False, 'pdf': False},
    # GPT-5.6 family: text + image input (no video/pdf), per the OpenAI model card.
    'gpt-5.6-luna': {'images': True, 'video': False, 'pdf': False},
    # The ChatGPT-subscription variants are the same underlying models, so they
    # carry the same capabilities as their API-key twins.
    'gpt-5.6-luna-chatgpt': {'images': True, 'video': False, 'pdf': False},
    'gpt-5.6-sol-chatgpt': {'images': True, 'video': False, 'pdf': False},

    # DeepSeek - primarily text focused
    'deepseek-v4-flash': {'images': False, 'video': False, 'pdf': False},
    'deepseek-v4-pro': {'images': False, 'video': False, 'pdf': False},
    # Same model as deepseek-v4-flash, served by GMI Cloud rather than
    # DeepSeek's own API — same (text-only) capabilities.
    'deepseek-v4-flash-gmi': {'images': False, 'video': False, 'pdf': False},
    # Same model again, served by Fireworks AI — same (text-only) capabilities.
    'deepseek-v4-flash-fireworks': {'images': False, 'video': False, 'pdf': False},

    # Muse Spark 1.2 (Meta) - fully multimodal: text, images, video, audio, PDF.
    # (Audio has no category of its own in get_file_category, so it isn't listed.)
    'muse-spark-1.2-contributor': {'images': True, 'video': True, 'pdf': True},

    # Qwen3.7 Plus (Alibaba) - multimodal agent model: images and video; PDFs
    # are ingested as page-images, not natively, so we leave pdf off.
    'qwen3.7-plus': {'images': True, 'video': True, 'pdf': False},
}


def get_model_capabilities(model_name: str) -> dict:
    """Get capabilities for a model, defaulting to Gemini if unknown (most permissive).

    NOTE: the permissive default is deliberate (see test_unknown_model_defaults_
    permissive) — the frontend image auto-switch depends on unknown models
    reporting vision-capable so it never spuriously moves an image off a model.
    The trade-off: during a frontend/backend version skew (the dropdown offers a
    model name the running backend hasn't loaded), this default reports
    images:True while get_llm's unknown-model branch falls back to text-only
    DeepSeek — so an attached image reaches a text-only model and 400s with
    ``unknown variant image_url, expected text``. Keep every real model listed
    explicitly above so only genuine skew/typos ever hit this default.
    """
    return MODEL_CAPABILITIES.get(model_name, {'images': True, 'video': True, 'pdf': True})


def get_file_category(mime_type: str) -> str:
    """Categorize a mime type into content category."""
    if mime_type.startswith('image/'):
        return 'images'
    elif mime_type.startswith('video/'):
        return 'video'
    elif mime_type == 'application/pdf':
        return 'pdf'
    return 'unknown'
