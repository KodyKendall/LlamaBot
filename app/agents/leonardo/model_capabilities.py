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

    # DeepSeek - primarily text focused
    'deepseek-v4-flash': {'images': False, 'video': False, 'pdf': False},
    'deepseek-v4-pro': {'images': False, 'video': False, 'pdf': False},

    # Qwen VL (Alibaba) - images and video; PDFs are ingested as page-images,
    # not natively, so we leave pdf off.
    'qwen3-vl-plus': {'images': True, 'video': True, 'pdf': False},
}


def get_model_capabilities(model_name: str) -> dict:
    """Get capabilities for a model, defaulting to Gemini if unknown (most permissive)."""
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
