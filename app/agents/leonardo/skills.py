"""File-based Agent Skills for Leonardo agents (the SKILL.md open standard).

A skill is a folder ``.leonardo/skills/<slug>/SKILL.md`` with YAML frontmatter
(``name`` + ``description``) followed by a markdown body. The model always sees
each skill's cheap ``name`` + ``description`` (rendered into the ``use_skill``
tool description) and only loads the full body **when it decides the skill is
relevant** — by calling ``use_skill``, whose return value dumps the SKILL.md
markdown into the conversation. That is "progressive disclosure": ~100 tokens
per skill until invoked, full body only on demand, then it persists in-thread.

This mirrors the file-based memory system in ``memory.py`` (same ``.leonardo``
anchor, same no-pyyaml frontmatter parsing, same write/delete idiom). Unlike
memory there is deliberately NO index file and NO system-prompt injection — the
discovery list lives in the ``use_skill`` tool description instead.
"""

import re
import shutil
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# cwd-relative, resolves to /app/app/.leonardo/skills at runtime — same anchor
# as memory.MEMORY_DIR.
SKILLS_DIR = ".leonardo/skills"
SKILL_FILENAME = "SKILL.md"

MAX_SKILLS = 100
MAX_SKILL_CONTENT_CHARS = 20000
# Cap the total size of the <available_skills> catalog rendered into the
# use_skill tool description (progressive-disclosure budget discipline).
MAX_CATALOG_CHARS = 4000


def sanitize_skill_slug(name: str) -> str:
    """Convert a skill name to a safe kebab-case directory slug."""
    slug = name.lower().strip()
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'[^a-z0-9\-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug or "skill"


def _skill_md_path(slug: str) -> Path:
    return Path(SKILLS_DIR) / slug / SKILL_FILENAME


def parse_skill_frontmatter(filepath: Path) -> Optional[dict]:
    """Read a SKILL.md and parse its YAML frontmatter (name, description) + body.

    Returns dict with keys: slug, name, description, body — or None if parsing
    fails. The slug is the parent directory name (the invocation key), not a
    frontmatter field, matching the open-standard "folder name is the id" rule.
    """
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Error reading skill file {filepath}: {e}")
        return None

    # Parse frontmatter between --- delimiters (same approach as memory.py).
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if not match:
        logger.warning(f"Skill file {filepath} has no valid frontmatter")
        return None

    frontmatter_text = match.group(1)
    body = match.group(2).strip()

    # Simple YAML parsing (avoid dependency on pyyaml).
    frontmatter = {}
    for line in frontmatter_text.split('\n'):
        line = line.strip()
        if ':' in line:
            key, _, value = line.partition(':')
            frontmatter[key.strip()] = value.strip()

    slug = filepath.parent.name
    name = frontmatter.get("name", slug)
    description = frontmatter.get("description", "")

    return {
        "slug": slug,
        "name": name,
        "description": description,
        "body": body,
    }


def list_all_skills() -> list[dict]:
    """Scan .leonardo/skills/*/SKILL.md and return parsed skill dicts (sorted by slug)."""
    skills_dir = Path(SKILLS_DIR)
    if not skills_dir.exists():
        return []

    skills = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        filepath = skill_dir / SKILL_FILENAME
        if not filepath.exists():
            continue
        parsed = parse_skill_frontmatter(filepath)
        if parsed:
            skills.append(parsed)
    return skills


def get_skill(slug: str) -> Optional[dict]:
    """Return the parsed skill dict for a slug, or None if not found."""
    filepath = _skill_md_path(slug)
    if not filepath.exists():
        return None
    return parse_skill_frontmatter(filepath)


def get_skill_body(slug: str) -> Optional[str]:
    """Return the full raw SKILL.md text (frontmatter + body) for a slug, or None.

    This is what ``use_skill`` dumps into the conversation — we return the whole
    file so the loaded skill is a complete, portable SKILL.md.
    """
    filepath = _skill_md_path(slug)
    if not filepath.exists():
        return None
    try:
        return filepath.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"Error reading skill body {filepath}: {e}")
        return None


def write_skill_file(
    name: str,
    description: str,
    content: str,
    slug: Optional[str] = None,
) -> str:
    """Create or overwrite a skill's SKILL.md. Returns the slug.

    Frontmatter (name, description) is rendered from the args; ``content`` is the
    markdown body. If ``slug`` is omitted it is derived from ``name``.
    Raises ValueError on validation errors.
    """
    if not name or not name.strip():
        raise ValueError("Skill name is required.")
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        raise ValueError(
            f"Skill content too long ({len(content)} chars). Max is {MAX_SKILL_CONTENT_CHARS}."
        )

    slug = sanitize_skill_slug(slug or name)

    existing = list_all_skills()
    is_update = any(s["slug"] == slug for s in existing)
    if not is_update and len(existing) >= MAX_SKILLS:
        raise ValueError(f"Skill limit reached ({MAX_SKILLS}). Delete unused skills first.")

    skill_dir = Path(SKILLS_DIR) / slug
    skill_dir.mkdir(parents=True, exist_ok=True)

    file_content = f"""---
name: {name.strip()}
description: {description.strip()}
---

{content.strip()}
"""
    (skill_dir / SKILL_FILENAME).write_text(file_content, encoding="utf-8")
    logger.info(f"Saved skill: {slug}")
    return slug


def edit_skill_file(slug: str, old_string: str, new_string: str) -> str:
    """Replace a unique ``old_string`` with ``new_string`` in a skill's SKILL.md.

    Returns the slug. Raises ValueError if the skill is missing, the text is not
    found, or it is not unique (mirrors edit_leonardo_md's guards).
    """
    filepath = _skill_md_path(slug)
    if not filepath.exists():
        raise ValueError(f"Skill '{slug}' does not exist.")

    content = filepath.read_text(encoding="utf-8")
    count = content.count(old_string)
    if count == 0:
        raise ValueError(
            "Could not find the specified text. Read the skill first to see exact contents."
        )
    if count > 1:
        raise ValueError(
            f"The text to replace appears {count} times. Provide more context to make it unique."
        )

    filepath.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
    logger.info(f"Edited skill: {slug}")
    return slug


def delete_skill_file(slug: str) -> bool:
    """Delete a skill directory. Returns True if deleted, False if not found."""
    skill_dir = Path(SKILLS_DIR) / slug
    if not skill_dir.exists():
        return False
    shutil.rmtree(skill_dir)
    logger.info(f"Deleted skill: {slug}")
    return True


def render_available_skills() -> str:
    """Render the ``<available_skills>`` catalog (``slug: description``) for the
    ``use_skill`` tool description.

    Cheap L1 metadata only — never the skill bodies. Length-capped so a large
    library cannot blow the context budget.
    """
    skills = list_all_skills()
    if not skills:
        return "(No skills are installed yet. Use write_skill to create one.)"

    lines = []
    total = 0
    for s in skills:
        desc = s["description"] or "(no description)"
        line = f"- {s['slug']}: {desc}"
        if total + len(line) > MAX_CATALOG_CHARS:
            lines.append(f"...and more (call list_skills to see all {len(skills)}).")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)
