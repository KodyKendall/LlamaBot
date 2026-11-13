"""Read an image from the Rails app for the model (LP-44).

Used by ``RequestHandler._build_message_content`` (an image attached from Uploaded
files arrives as a path, not data) and by the rails agent's ``view_image`` tool.

The path comes from the client or the model, so it is untrusted:

* relative only, no ``..`` segments, resolved with symlinks followed, and the real
  file must sit under one of :data:`ALLOWED_DIRS` of the Rails app;
* the type comes from the file's first bytes (PNG, JPEG, GIF, WebP), never from the
  extension or the client's mime hint;
* images over :data:`MAX_SIDE` px or :data:`MAX_BYTES` are downscaled before encoding.

Whether to read at all is :func:`image_gate`: the operator's vision switch, the ZDR
model rule, and the model's own capability, in that order.

One log line per image (resolved / downscaled / refused / skipped), never its bytes.
"""
import base64
import io
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Optional

logger = logging.getLogger(__name__)

RAILS_ROOT = Path("/app/app/rails")

# Where uploaded and app images live. Everything else (config/, db/, .env) is refused.
ALLOWED_DIRS = ("app/assets/images", "app/imports", "public")

MAX_SIDE = 2000
MAX_BYTES = 4 * 1024 * 1024
# Files larger than this are not even opened (a decompression-bomb guard on top of
# Pillow's own pixel limit).
MAX_SOURCE_BYTES = 50 * 1024 * 1024

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class ImageRefused(Exception):
    """The path or file may not be sent. ``str(e)`` is the reason, safe to log."""


def _box() -> str:
    return os.getenv("INSTANCE_NAME", "?")


def sniff_image_type(head: bytes) -> Optional[str]:
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve_app_image(path: str) -> Path:
    """The real file for an app-relative image path, or raise :class:`ImageRefused`."""
    if not path or not isinstance(path, str):
        raise ImageRefused("empty path")
    if path.startswith(("/", "~")) or "\\" in path or "\x00" in path:
        raise ImageRefused("absolute path")
    parts = PurePosixPath(path).parts
    if ".." in parts:
        raise ImageRefused("outside the app (..)")

    root = Path(os.path.realpath(RAILS_ROOT))
    real = Path(os.path.realpath(root / path))
    allowed = [Path(os.path.realpath(root / d)) for d in ALLOWED_DIRS]
    if not any(real == base or base in real.parents for base in allowed):
        if root != real and root not in real.parents:
            raise ImageRefused("outside the app (symlink)")
        raise ImageRefused("folder not allowed")
    if not real.is_file():
        raise ImageRefused("not found")
    if real.stat().st_size > MAX_SOURCE_BYTES:
        raise ImageRefused("file too large")
    with open(real, "rb") as f:
        if sniff_image_type(f.read(16)) is None:
            raise ImageRefused("not an image")
    return real


def _fit(raw: bytes, mime: str):
    """(bytes, mime, (w, h) original, (w, h) sent) — downscaled when over the limits."""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        original = im.size
        if max(original) <= MAX_SIDE and len(raw) <= MAX_BYTES:
            return raw, mime, original, original
        im.seek(0)
        frame = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
    frame.thumbnail((MAX_SIDE, MAX_SIDE))

    keep = {"image/png": "PNG", "image/webp": "WEBP"}.get(mime, "JPEG")
    out_mime = {"PNG": "image/png", "WEBP": "image/webp", "JPEG": "image/jpeg"}[keep]
    for attempt in range(16):
        buf = io.BytesIO()
        if keep == "JPEG":
            frame.convert("RGB").save(buf, format="JPEG", quality=max(40, 85 - attempt * 15))
        else:
            frame.save(buf, format=keep, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_BYTES or min(frame.size) <= 32:
            return data, out_mime, original, frame.size
        # Still too heavy (noise compresses badly): JPEG, then smaller each round.
        keep, out_mime = "JPEG", "image/jpeg"
        if attempt >= 2:
            frame.thumbnail((max(1, int(frame.size[0] * 0.75)), max(1, int(frame.size[1] * 0.75))))
    return data, out_mime, original, frame.size


def load_app_image(path: str, model: str = "") -> dict:
    """Resolve, read and (if needed) downscale. Raises :class:`ImageRefused`.

    Returns ``{"mime_type", "data" (base64), "path"}``.
    """
    try:
        real = resolve_app_image(path)
    except ImageRefused as refused:
        logger.warning(f"LP-44 image refused: path={path!r} reason={refused} model={model} box={_box()}")
        raise
    raw = real.read_bytes()
    mime = sniff_image_type(raw[:16])
    try:
        data, mime, original, sent = _fit(raw, mime)
    except Exception as e:  # noqa: BLE001 - a corrupt image is a refusal, not a crash
        logger.warning(f"LP-44 image refused: path={path!r} reason=unreadable image ({e}) "
                       f"model={model} box={_box()}")
        raise ImageRefused("unreadable image") from None
    if sent != original or len(data) != len(raw):
        logger.info(f"LP-44 image downscaled: path={path!r} model={model} "
                    f"{original[0]}x{original[1]} {len(raw)}B -> {sent[0]}x{sent[1]} {len(data)}B")
    else:
        logger.info(f"LP-44 image resolved: path={path!r} model={model} "
                    f"{original[0]}x{original[1]} {len(raw)}B")
    return {"mime_type": mime, "data": base64.b64encode(data).decode(), "path": path}


def image_gate(model: str) -> Optional[str]:
    """Why an image may NOT go to ``model``: "vision off", "zdr", "text-only"; or None."""
    from app.agents.leonardo import zdr
    from app.agents.leonardo.model_capabilities import get_model_capabilities
    from app.agents.leonardo.model_policy import vision_allowed

    if not vision_allowed():
        return "vision off"
    if zdr.enforced() and not zdr.is_zdr_compliant(model):
        return "zdr"
    if not get_model_capabilities(model).get("images", False):
        return "text-only"
    return None


def log_skipped(path: str, model: str, reason: str) -> None:
    logger.info(f"LP-44 image skipped: path={path!r} reason={reason} model={model} box={_box()}")


def cannot_see_note(model: str) -> str:
    return f"[Note: an image was attached but {model} cannot see images, so it was not shown.]"
