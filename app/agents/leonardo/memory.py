"""File-based memory system for Leonardo agent.

Stores memories as markdown files with YAML frontmatter in .leonardo/memory/.
Auto-generates a MEMORY.md index file for injection into agent system prompts.
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MEMORY_DIR = ".leonardo/memory"
MEMORY_MD_PATH = ".leonardo/MEMORY.md"
MAX_MEMORIES = 50
MAX_MEMORY_CONTENT_CHARS = 2000

VALID_MEMORY_TYPES = ["user", "feedback", "project", "reference"]

MEMORY_TYPE_LABELS = {
    "user": "User Preferences",
    "feedback": "Feedback & Corrections",
    "project": "Project Context",
    "reference": "References",
}


def sanitize_memory_filename(name: str) -> str:
    """Convert a memory name to a safe filename."""
    # Lowercase, replace spaces/underscores with hyphens, strip non-alphanumeric
    filename = name.lower().strip()
    filename = re.sub(r'[\s_]+', '-', filename)
    filename = re.sub(r'[^a-z0-9\-]', '', filename)
    filename = re.sub(r'-+', '-', filename).strip('-')
    if not filename:
        filename = "memory"
    return filename + ".md"


def parse_memory_frontmatter(filepath: Path) -> Optional[dict]:
    """Read a memory file and parse its YAML frontmatter.

    Returns dict with keys: name, description, type, content, filename
    or None if parsing fails.
    """
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Error reading memory file {filepath}: {e}")
        return None

    # Parse frontmatter between --- delimiters
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if not match:
        logger.warning(f"Memory file {filepath} has no valid frontmatter")
        return None

    frontmatter_text = match.group(1)
    content = match.group(2).strip()

    # Simple YAML parsing (avoid dependency on pyyaml)
    frontmatter = {}
    for line in frontmatter_text.split('\n'):
        line = line.strip()
        if ':' in line:
            key, _, value = line.partition(':')
            frontmatter[key.strip()] = value.strip()

    name = frontmatter.get("name", filepath.stem)
    description = frontmatter.get("description", "")
    memory_type = frontmatter.get("type", "project")

    return {
        "name": name,
        "description": description,
        "type": memory_type,
        "content": content,
        "filename": filepath.name,
    }


def list_all_memories() -> list[dict]:
    """Scan .leonardo/memory/*.md and return list of parsed memory dicts."""
    memory_dir = Path(MEMORY_DIR)
    if not memory_dir.exists():
        return []

    memories = []
    for filepath in sorted(memory_dir.glob("*.md")):
        parsed = parse_memory_frontmatter(filepath)
        if parsed:
            memories.append(parsed)

    return memories


def rebuild_memory_index():
    """Regenerate .leonardo/MEMORY.md from all memory files."""
    memories = list_all_memories()

    if not memories:
        # Remove index if no memories exist
        index_path = Path(MEMORY_MD_PATH)
        if index_path.exists():
            index_path.unlink()
        return

    # Group by type
    grouped = {}
    for mem in memories:
        t = mem["type"]
        if t not in grouped:
            grouped[t] = []
        grouped[t].append(mem)

    lines = ["# Leonardo Memories\n"]

    for memory_type in VALID_MEMORY_TYPES:
        if memory_type not in grouped:
            continue
        label = MEMORY_TYPE_LABELS.get(memory_type, memory_type)
        lines.append(f"\n## {label}\n")
        for mem in grouped[memory_type]:
            lines.append(f"### {mem['name']} ({mem['filename']})")
            lines.append(f"_{mem['description']}_\n")
            lines.append(mem['content'])
            lines.append("")

    # Write index
    index_path = Path(MEMORY_MD_PATH)
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"Rebuilt MEMORY.md with {len(memories)} memories")


def write_memory_file(name: str, description: str, memory_type: str, content: str) -> str:
    """Write a memory file with frontmatter and rebuild index.

    Returns the filename on success.
    Raises ValueError on validation errors.
    """
    if memory_type not in VALID_MEMORY_TYPES:
        raise ValueError(f"Invalid memory type '{memory_type}'. Must be one of: {VALID_MEMORY_TYPES}")

    if len(content) > MAX_MEMORY_CONTENT_CHARS:
        raise ValueError(f"Memory content too long ({len(content)} chars). Max is {MAX_MEMORY_CONTENT_CHARS}.")

    # Check memory count limit
    existing = list_all_memories()
    filename = sanitize_memory_filename(name)
    # Don't count against limit if overwriting existing file
    is_update = any(m["filename"] == filename for m in existing)
    if not is_update and len(existing) >= MAX_MEMORIES:
        raise ValueError(f"Memory limit reached ({MAX_MEMORIES}). Delete old memories before saving new ones.")

    # Ensure directory exists
    memory_dir = Path(MEMORY_DIR)
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Write file
    file_content = f"""---
name: {name}
description: {description}
type: {memory_type}
---

{content}
"""
    filepath = memory_dir / filename
    filepath.write_text(file_content, encoding="utf-8")
    logger.info(f"Saved memory: {filename}")

    rebuild_memory_index()
    return filename


def delete_memory_file(filename: str) -> bool:
    """Delete a memory file and rebuild index.

    Returns True if deleted, False if not found.
    """
    # The LLM can call this with junk. Every reject must return False (the tool turns
    # that into a "not found" ToolMessage) rather than raise — a raise propagates
    # through LangGraph's default tool-error handler and kills the whole chat turn.
    if not filename or not str(filename).strip():
        return False

    base = Path(MEMORY_DIR).resolve()
    try:
        filepath = (base / str(filename).strip()).resolve()
    except (OSError, ValueError):
        return False

    # Confine to the memory dir (blocks "../escape.md" and absolute paths) and only
    # ever unlink a regular file (blocks "" / a subdirectory → IsADirectoryError).
    if base not in filepath.parents or not filepath.is_file():
        return False

    filepath.unlink()
    logger.info(f"Deleted memory: {filename}")
    rebuild_memory_index()
    return True
