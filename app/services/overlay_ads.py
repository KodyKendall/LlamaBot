"""Promo ("jumbotron") slot for the building overlay.

The mothership owns the creative: it stores HTML snippets and hands them to
instances over ``POST /api/leonardo/overlay_ads``. LlamaBot only carries the
plumbing — fetch, sanity-check, cache briefly, hand to the browser — so a promo
can be written/edited on the mothership and picked up on the next build without
touching an instance.

Two rules this module exists to enforce:

1. **Fail open, always.** No mothership, an old mothership that 404s the
   endpoint, a timeout, garbage JSON — all of it degrades to "no promos", never
   to a broken overlay. Nothing here raises.
2. **Bound what a remote payload can do.** The snippets are rendered by the
   browser, so the count, the per-snippet size, the slot height and the rotation
   cadence are all clamped here rather than trusted. (The browser renders each
   snippet in a *sandboxed, opaque-origin* iframe — see ``OverlayAds.js`` — so
   the HTML itself can never reach the chat DOM, cookies or session.)
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Operator override, same directory as instance.json. When present it WINS over
#: the mothership — that's the point: drop a file here to preview promo HTML on a
#: box before the creative is published fleet-wide (and it's how this feature is
#: QA'd at all on a box whose mothership hasn't shipped the endpoint). Same
#: payload shape as the mothership response; the clamps below still apply.
LOCAL_OVERRIDE_PATH = ".leonardo/overlay_ads.json"

#: Hard ceilings on anything a remote payload can ask the overlay to do.
MAX_ADS = 10
MAX_AD_BYTES = 64 * 1024

DEFAULT_ROTATE_SECONDS = 180
MIN_ROTATE_SECONDS = 15
MAX_ROTATE_SECONDS = 3600

DEFAULT_AD_HEIGHT = 140
MIN_AD_HEIGHT = 60
MAX_AD_HEIGHT = 400

# ---------------------------------------------------------------------------
# Display policy — WHEN the jumbotron shows, as opposed to WHAT it shows.
#
# This lives on the mothership on purpose: which builds get a promo, how long we
# wait before showing one, and how often a given browser sees one are all things
# worth experimenting with and personalising per user, and none of them should
# need an image build to change. Everything here is a *default* the mothership's
# `policy` object overrides field by field, so an old mothership (or one that
# sends no policy at all) still gets working behaviour.
#
# The instance keeps two guarantees regardless of what the mothership sends:
#   1. Never during a question — Leo is blocked on the user; nothing competes.
#      That's why "question" is not an offerable mode below.
#   2. Every value is clamped. A policy can tune the product, not break it.
# ---------------------------------------------------------------------------

#: Layouts a promo may appear in. "question" is deliberately absent — see above.
SELECTABLE_MODES = ("building", "plan")

#: Hold the promo back for the first few seconds of a build. Without this a short
#: build flashes an ad for two seconds and yanks it away, which reads as a bug.
DEFAULT_SHOW_AFTER_SECONDS = 5
MAX_SHOW_AFTER_SECONDS = 300

#: Per-browser cooldown between promos. 0 = show on every build (the default;
#: turn it up from the mothership if promos start feeling relentless).
DEFAULT_MIN_INTERVAL_SECONDS = 0
MAX_MIN_INTERVAL_SECONDS = 86400

DEFAULT_POLICY = {
    "enabled": True,
    "show_after_seconds": DEFAULT_SHOW_AFTER_SECONDS,
    "modes": list(SELECTABLE_MODES),
    "min_interval_seconds": DEFAULT_MIN_INTERVAL_SECONDS,
}

#: How long a fetched payload is reused. Short enough that editing a promo on the
#: mothership shows up on the next build or two (the point of the feature), long
#: enough that a user hammering Enter doesn't hammer the mothership.
CACHE_TTL_SECONDS = 60

def empty() -> dict:
    """A fresh "nothing to show" payload. A function, not a constant, so callers
    can never mutate the shared default out from under each other."""
    return {
        "ads": [],
        "rotate_seconds": DEFAULT_ROTATE_SECONDS,
        "policy": dict(DEFAULT_POLICY),
        "variant": None,
    }


#: Back-compat alias for the tests/callers that only compare shapes.
EMPTY = empty()

_cache: dict[str, tuple[float, dict]] = {}


def _clamp(value, low, high, default):
    """Coerce ``value`` to an int inside [low, high]; ``default`` if it isn't one."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def normalize(payload) -> dict:
    """Turn a raw mothership payload into the shape the overlay consumes.

    Always returns ``{"ads": [...], "rotate_seconds": int}``. Malformed entries
    are dropped individually — one bad snippet must not blank the whole slot.
    """
    if not isinstance(payload, dict):
        return empty()

    raw_ads = payload.get("ads")
    if not isinstance(raw_ads, list):
        return empty()

    ads = []
    for index, entry in enumerate(raw_ads):
        if len(ads) >= MAX_ADS:
            logger.info(f"Overlay ads truncated to {MAX_ADS}; mothership sent {len(raw_ads)}")
            break
        if not isinstance(entry, dict):
            continue
        html = entry.get("html")
        if not isinstance(html, str) or not html.strip():
            continue
        if len(html.encode("utf-8")) > MAX_AD_BYTES:
            # Dropped, never truncated: a half-snippet is broken markup.
            logger.info(f"Overlay ad {entry.get('id', index)!r} dropped (over {MAX_AD_BYTES} bytes)")
            continue
        ad_id = entry.get("id")
        ads.append({
            "id": str(ad_id) if ad_id else f"ad-{index}",
            "html": html,
            "height": _clamp(entry.get("height"), MIN_AD_HEIGHT, MAX_AD_HEIGHT, DEFAULT_AD_HEIGHT),
        })

    variant = payload.get("variant")
    return {
        "ads": ads,
        "rotate_seconds": _clamp(
            payload.get("rotate_seconds"),
            MIN_ROTATE_SECONDS,
            MAX_ROTATE_SECONDS,
            DEFAULT_ROTATE_SECONDS,
        ),
        "policy": normalize_policy(payload.get("policy")),
        # Opaque experiment/segment label. The instance does nothing with it
        # beyond echoing it into `leoAds.status()`, so whoever is running the
        # experiment can see which arm a browser actually got.
        "variant": str(variant) if variant else None,
    }


def normalize_policy(raw) -> dict:
    """Merge the mothership's `policy` over the baked-in defaults, clamped.

    Every field is independently optional: sending `{"show_after_seconds": 20}`
    changes only the delay and leaves the rest at their defaults. A policy that
    is missing, null, or not an object gives the defaults untouched.
    """
    policy = dict(DEFAULT_POLICY)
    if not isinstance(raw, dict):
        return policy

    if "enabled" in raw:
        policy["enabled"] = bool(raw.get("enabled"))

    if "show_after_seconds" in raw:
        policy["show_after_seconds"] = _clamp(
            raw.get("show_after_seconds"), 0, MAX_SHOW_AFTER_SECONDS,
            DEFAULT_SHOW_AFTER_SECONDS,
        )

    if "min_interval_seconds" in raw:
        policy["min_interval_seconds"] = _clamp(
            raw.get("min_interval_seconds"), 0, MAX_MIN_INTERVAL_SECONDS,
            DEFAULT_MIN_INTERVAL_SECONDS,
        )

    if "modes" in raw:
        wanted = raw.get("modes")
        if isinstance(wanted, list):
            # Intersect rather than trust: an unknown mode is dropped, and
            # "question" can never be selected (Leo is blocked on the user).
            modes = [m for m in SELECTABLE_MODES if m in wanted]
            dropped = [m for m in wanted if m not in SELECTABLE_MODES]
            if dropped:
                logger.info(f"Overlay ad policy: ignoring unselectable modes {dropped}")
            policy["modes"] = modes

    return policy


def local_override() -> Optional[dict]:
    """Normalized payload from ``LOCAL_OVERRIDE_PATH``, or ``None`` if there isn't
    a usable one. Never raises — a typo'd override file falls back to the
    mothership rather than blanking the overlay.
    """
    try:
        path = Path(LOCAL_OVERRIDE_PATH)
        if not path.is_file():
            return None
        return normalize(json.loads(path.read_text()))
    except Exception as e:
        logger.warning(f"Ignoring unreadable {LOCAL_OVERRIDE_PATH}: {e}")
        return None


def cached(key: str = "anon") -> Optional[dict]:
    """The last normalized payload for ``key`` if it's still fresh, else ``None``.

    Keyed per user because the mothership personalises both the creative and the
    display policy. A single process-wide cache would hand the first requester's
    (possibly targeted) promos to everyone else signed into the same box.
    """
    entry = _cache.get(key)
    if entry is None:
        return None
    fetched_at, payload = entry
    if time.monotonic() - fetched_at > CACHE_TTL_SECONDS:
        return None
    return payload


def store(payload: dict, key: str = "anon") -> dict:
    """Cache a normalized payload under ``key`` and return it."""
    _cache[key] = (time.monotonic(), payload)
    return payload


def clear_cache() -> None:
    """Drop the cache (tests, and any future 'refresh promos now' control)."""
    _cache.clear()
