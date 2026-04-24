"""Shared project context loader for Leonardo agents.

Loads .leonardo/LEONARDO.md and .leonardo/MEMORY.md and appends them to system prompts.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Path to LEONARDO.md - same location as instance.json
LEONARDO_MD_PATH = ".leonardo/LEONARDO.md"
MEMORY_MD_PATH = ".leonardo/MEMORY.md"


def get_leonardo_md_content() -> Optional[str]:
    """Load LEONARDO.md content if it exists.

    Returns:
        The file content as a string, or None if file doesn't exist or is empty.
    """
    if not os.path.exists(LEONARDO_MD_PATH):
        return None

    try:
        with open(LEONARDO_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            logger.debug(f"Loaded LEONARDO.md ({len(content)} chars)")
            return content
        return None
    except Exception as e:
        logger.warning(f"Error reading LEONARDO.md: {e}")
        return None


def get_memory_md_content() -> Optional[str]:
    """Load MEMORY.md content if it exists.

    Returns:
        The file content as a string, or None if file doesn't exist or is empty.
    """
    if not os.path.exists(MEMORY_MD_PATH):
        return None

    try:
        with open(MEMORY_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            logger.debug(f"Loaded MEMORY.md ({len(content)} chars)")
            return content
        return None
    except Exception as e:
        logger.warning(f"Error reading MEMORY.md: {e}")
        return None


def build_system_prompt_with_project_context(
    base_prompt: str,
    suffix: str = ""
) -> str:
    """Build a complete system prompt with optional project context and memories.

    Args:
        base_prompt: The agent's base system prompt
        suffix: Optional suffix to append after project context (e.g., date)

    Returns:
        Complete system prompt string with project context and memories appended
    """
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
