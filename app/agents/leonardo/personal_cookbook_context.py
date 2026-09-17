"""The box owner's personal cookbook, as a RUNTIME PROMPT ADDENDUM.

Why this is not just three paragraphs in ``prompts.py`` (which is where it was):

``project_context.resolve_base_prompt()`` prefers the mothership-delivered cached body
whenever it is at least ``MIN_PROMPT_LEN`` long, and the mothership serves nine prompt
files that were seeded byte-for-byte from LlamaBot **0.4.1** on 2026-06-27 and have not
been touched since. Measured 2026-09-11 on a customer's own box (``leo-loepo``, llamabot
0.7.7), table ``agent_system_prompts``: all nine rows contain ``PERSONAL cookbook`` zero
times, ``@cookbook`` zero times, and ``publish-to-your-personal-cookbook`` zero times.
The one cookbook sentence that survives is ``curl https://llamapress.ai/cookbook.json``.

So the cookbook paragraphs added in 0.7.5, and the credentials fix added in 0.7.7, have
never reached a single box. Richie (vicetheorystudios) published
``lionhearted-metallic-gold`` from one Leo and could only use it on his other Leo by
opening the ``/cookbook`` slash menu and picking it by hand — asking for it in words did
nothing, because the agent had never been told a personal cookbook exists.

An **addendum** appended after ``resolve_base_prompt()`` returns survives the override,
the same way ``LEONARDO.md`` and ``MEMORY.md`` already do. A prompt edit does not.

Two hard rules hold this module together:

1. **No network call on the prompt path.** Prompt building is synchronous and runs on
   every turn; a 5-second httpx call there would add latency to every message and could
   hang a turn outright. ``build_personal_cookbook_context()`` reads a module-level cache
   and nothing else. A cold cache emits nothing and schedules a background refresh, so
   the block appears on a later turn.
2. **It can never raise.** This is an enrichment. Breaking a turn, or emptying the slash
   menu, would be far worse than the user having to use the menu.

The cache lives here rather than in ``app/routers/api.py`` so the slash-menu endpoint and
the prompt builder share one source of truth; ``api.py`` imports it.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

#: The owner's personal list changes the moment they publish, so it gets its own
#: short-TTL cache rather than riding the process-global fleet cookbook cache.
PERSONAL_COOKBOOK_CACHE_TTL_SECONDS = 60

#: Budget. A user with hundreds of recipes must not silently eat the context window.
MAX_RECIPES = 40
MAX_BLOCK_CHARS = 4096
MAX_SUMMARY_CHARS = 140
MAX_TITLE_CHARS = 90

_cache: dict = {}
_refresh_in_flight = False

INDEX_URL = "https://llamapress.ai/cookbook/u/{handle}"
RECIPE_URL = "https://llamapress.ai/cookbook/u/{handle}/{slug}"
PUBLISH_GUIDE_URL = "https://llamapress.ai/cookbook/publish-to-your-personal-cookbook.md"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def reset_personal_cookbook_cache() -> None:
    """Drop everything. Tests use this; nothing in production should need it."""
    global _refresh_in_flight
    _cache.clear()
    _refresh_in_flight = False


def expire_personal_cookbook_cache() -> None:
    """Keep the recipes but mark them stale, so the next refresh actually fetches."""
    _cache["fetched_at"] = 0.0


def store_personal_recipes(guides: list) -> None:
    _cache["guides"] = guides
    _cache["fetched_at"] = time.monotonic()


def get_cached_personal_recipes() -> Optional[list]:
    """The cached guides, or ``None`` on a cold cache. Does not fetch."""
    return _cache.get("guides")


def personal_cookbook_cache_is_fresh() -> bool:
    if _cache.get("guides") is None:
        return False
    return (time.monotonic() - _cache.get("fetched_at", 0.0)) < PERSONAL_COOKBOOK_CACHE_TTL_SECONDS


def normalize_personal_recipes(payload) -> list:
    """Shape the owner's recipes into the same guide dict the slash menu already consumes.

    Unlisted recipes are kept: they are the owner's own, and hiding them here would mean a
    user could not find a recipe they had just published. Entries without a slug, or a
    payload with no handle, are dropped — there is no resolvable URL for either.
    """
    if not isinstance(payload, dict):
        return []
    handle = str(payload.get("handle") or "").strip()
    raw = payload.get("recipes")
    if not handle or not isinstance(raw, list):
        return []

    guides = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or "").strip()
        if not slug:
            continue
        guides.append({
            "slug": slug,
            "title": str(entry.get("title") or slug),
            "category": str(entry.get("category") or ""),
            "summary": str(entry.get("summary") or ""),
            # Both .json and .md of this URL exist, so the frontend's existing
            # cookbookJsonUrl mention mechanics work unchanged.
            "url": RECIPE_URL.format(handle=handle, slug=slug),
            "handle": handle,
            "visibility": str(entry.get("visibility") or ""),
            "updated_at": str(entry.get("updated_at") or ""),
            "personal": True,
        })
    return guides


# ---------------------------------------------------------------------------
# Refresh (async — never called from the prompt path)
# ---------------------------------------------------------------------------

async def refresh_personal_cookbook(client) -> list:
    """Fetch and cache the owner's recipes. Never raises; an empty list is the floor.

    A failed fetch KEEPS the last good answer rather than blanking it: a network blip
    must not make a user's own recipes vanish from their slash menu mid-session.
    """
    if personal_cookbook_cache_is_fresh():
        return _cache.get("guides") or []

    if client is None:
        return _cache.get("guides") or []

    try:
        guides = normalize_personal_recipes(await client.get_personal_cookbook())
    except Exception as e:  # noqa: BLE001 - the menu and the prompt must survive this
        logger.warning(f"Could not fetch personal cookbook: {e}")
        return _cache.get("guides") or []

    store_personal_recipes(guides)
    return guides


def _build_client():
    """A MothershipClient for the background refresh, or None if there is no config.

    Constructed lazily and locally: the prompt path has no ``request`` to read
    ``app.state.mothership_client`` off, and importing main.py from here would be a cycle.
    """
    try:
        from app.services.mothership_client import MothershipClient

        return MothershipClient()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"No mothership client for personal cookbook refresh: {e}")
        return None


def schedule_personal_cookbook_refresh() -> None:
    """Kick a refresh WITHOUT awaiting it, if an event loop is running.

    Fire-and-forget on purpose: the caller is the synchronous prompt builder, which must
    return at once. Outside a loop (a plain sync context, a test) this is a no-op, which
    is why a cold cache simply emits no block and tries again on the next turn.
    """
    global _refresh_in_flight

    if _refresh_in_flight or personal_cookbook_cache_is_fresh():
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    client = _build_client()
    if client is None:
        return

    async def _run():
        global _refresh_in_flight
        try:
            await refresh_personal_cookbook(client)
        finally:
            _refresh_in_flight = False

    _refresh_in_flight = True
    try:
        loop.create_task(_run())
    except Exception as e:  # noqa: BLE001
        _refresh_in_flight = False
        logger.debug(f"Could not schedule personal cookbook refresh: {e}")


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------

def _sort_key(guide: dict) -> str:
    """Newest first, so a truncated list keeps what the user just published."""
    return str(guide.get("updated_at") or "")


def _line(guide: dict) -> str:
    title = str(guide.get("title") or guide.get("slug") or "")[:MAX_TITLE_CHARS]
    summary = " ".join(str(guide.get("summary") or "").split())[:MAX_SUMMARY_CHARS]
    tail = f": {summary}" if summary else ""
    return f"- `{guide['slug']}` — {title}{tail} → {guide['url']}.md\n"


def build_personal_cookbook_context() -> str:
    """The addendum, or ``""`` when this box's owner has published nothing.

    Emitting nothing is the common case and it must cost zero tokens — most boxes have an
    owner who has never published a recipe.
    """
    try:
        guides = get_cached_personal_recipes()
    except Exception as e:  # noqa: BLE001 - an enrichment may never break a turn
        logger.warning(f"Could not read personal cookbook cache: {e}")
        return ""

    if not guides:
        return ""

    try:
        handle = next((g.get("handle") for g in guides if g.get("handle")), "")
        if not handle:
            return ""

        ordered = sorted(guides, key=_sort_key, reverse=True)[:MAX_RECIPES]

        header = (
            "\n\n---\n\n"
            "# Your Personal Cookbook (recipes this user published from their own Leos)\n\n"
            f"Index: {INDEX_URL.format(handle=handle)} (add .json for the machine list)\n\n"
        )
        footer = (
            "\nWhen the user refers to something they saved, built on another Leo, or "
            '"their" pattern/style/component, curl the matching `.md` above and follow it '
            "before inventing anything. An `@cookbook:<slug> (url)` mention in their "
            "message means they picked that recipe — curl that URL first. To SAVE a new "
            f"pattern, follow {PUBLISH_GUIDE_URL} — it documents where this box's "
            "credentials live; do not read them from the environment.\n"
        )

        budget = MAX_BLOCK_CHARS - len(header) - len(footer)
        lines = []
        for guide in ordered:
            line = _line(guide)
            if budget - len(line) < 0:
                break
            budget -= len(line)
            lines.append(line)

        if not lines:
            return ""

        return header + "".join(lines) + footer
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not render personal cookbook context: {e}")
        return ""


def personal_cookbook_section() -> str:
    """What the prompt builders call: schedule a refresh, then render what we have."""
    try:
        schedule_personal_cookbook_refresh()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Personal cookbook refresh scheduling failed: {e}")
    return build_personal_cookbook_context()
