"""Shared project context loader for Leonardo agents.

Loads .leonardo/ workspace files and appends them to system prompts.
"""

import os
import logging
from typing import Optional

from app.services import system_prompt_cache

logger = logging.getLogger(__name__)

# A real base prompt is always far longer than this. Used as a corruption guard
# when adopting a mothership-delivered override (see resolve_base_prompt).
MIN_PROMPT_LEN = 200

# Workspace file paths
LEONARDO_MD_PATH = ".leonardo/LEONARDO.md"
MEMORY_MD_PATH = ".leonardo/MEMORY.md"
SOUL_MD_PATH = ".leonardo/SOUL.md"
USER_MD_PATH = ".leonardo/USER.md"
IDENTITY_MD_PATH = ".leonardo/IDENTITY.md"


def _load_md_file(path: str, label: str) -> Optional[str]:
    """Load a markdown file if it exists and is non-empty."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            logger.debug(f"Loaded {label} ({len(content)} chars)")
            return content
        return None
    except Exception as e:
        logger.warning(f"Error reading {label}: {e}")
        return None


def get_leonardo_md_content() -> Optional[str]:
    return _load_md_file(LEONARDO_MD_PATH, "LEONARDO.md")


def get_memory_md_content() -> Optional[str]:
    return _load_md_file(MEMORY_MD_PATH, "MEMORY.md")


def get_soul_md_content() -> Optional[str]:
    return _load_md_file(SOUL_MD_PATH, "SOUL.md")


def get_user_md_content() -> Optional[str]:
    return _load_md_file(USER_MD_PATH, "USER.md")


def get_identity_md_content() -> Optional[str]:
    return _load_md_file(IDENTITY_MD_PATH, "IDENTITY.md")




def resolve_base_prompt(static_default: str, agent_mode: Optional[str]) -> str:
    """Resolve the base prompt, preferring a mothership-delivered cached override.

    Fail-open protects against ABSENCE (no override / unreachable mothership →
    static prompt). This guard additionally protects against CORRUPTION/TRUNCATION:
    only adopt an override that is non-empty and plausibly complete, otherwise a
    short/garbled body would silently replace the real prompt for the whole fleet
    on every new run. When in doubt, keep the baked-in static prompt.

    ``agent_mode=None`` short-circuits to the static default — preserving today's
    behavior exactly for callers that don't pass a mode.
    """
    if agent_mode:
        cached = system_prompt_cache.get_cached(agent_mode)  # None on miss / any error
        if cached and len(cached) >= MIN_PROMPT_LEN:
            logger.info(
                f"Using mothership-delivered system prompt for agent_mode={agent_mode} "
                f"({len(cached)} chars)"
            )
            return cached
    return static_default


def build_system_prompt_with_project_context(
    base_prompt: str,
    suffix: str = "",
    agent_mode: Optional[str] = None,
) -> str:
    """Build a complete system prompt with optional project context and memories.

    Used by non-beginner agents. Does NOT include personality files or bootstrap.
    When ``agent_mode`` is given, a mothership-delivered prompt override (if any)
    replaces ``base_prompt`` before the project context overlay is appended.
    """
    base_prompt = resolve_base_prompt(base_prompt, agent_mode)
    leonardo_md = get_leonardo_md_content()
    memory_md = get_memory_md_content()

    parts = [base_prompt]

    if leonardo_md:
        parts.append("\n\n---\n\n# Project Context (from LEONARDO.md)\n\n")
        parts.append(leonardo_md)

    if memory_md:
        parts.append("\n\n---\n\n# Agent Memories (from MEMORY.md)\n\n")
        parts.append(memory_md)

    if suffix:
        parts.append(suffix)

    return "".join(parts)


def build_beginner_system_prompt(
    base_prompt: str,
    suffix: str = "",
    agent_mode: Optional[str] = None,
) -> str:
    """Build system prompt for beginner agent with personality files.

    Injection order: IDENTITY → SOUL → base prompt → USER → LEONARDO → MEMORY
    When ``agent_mode`` is given, a mothership-delivered prompt override (if any)
    replaces ``base_prompt`` before the personality/context overlays are applied.
    """
    base_prompt = resolve_base_prompt(base_prompt, agent_mode)
    identity_md = get_identity_md_content()
    soul_md = get_soul_md_content()
    user_md = get_user_md_content()
    leonardo_md = get_leonardo_md_content()
    memory_md = get_memory_md_content()

    parts = []

    # Identity and soul come BEFORE base prompt — agent "is" someone first
    if identity_md:
        parts.append("# Agent Identity (from IDENTITY.md)\n\n")
        parts.append(identity_md)
        parts.append("\n\n---\n\n")

    if soul_md:
        parts.append("# Agent Soul (from SOUL.md)\n\n")
        parts.append(soul_md)
        parts.append("\n\n---\n\n")

    # Base prompt (the beginner agent instructions)
    parts.append(base_prompt)

    # User context comes after base prompt
    if user_md:
        parts.append("\n\n---\n\n# About the User (from USER.md)\n\n")
        parts.append(user_md)

    # Project plan and memories (same as non-beginner)
    if leonardo_md:
        parts.append("\n\n---\n\n# Project Context (from LEONARDO.md)\n\n")
        parts.append(leonardo_md)

    if memory_md:
        parts.append("\n\n---\n\n# Agent Memories (from MEMORY.md)\n\n")
        parts.append(memory_md)

    if suffix:
        parts.append(suffix)

    return "".join(parts)
