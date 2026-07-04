"""Shared persistence for the Brand Guide.

The brand guide is one small, user-editable branding guideline stored under
``.leonardo/`` as TWO files:

  - ``brand.json`` — the structured source of truth (the editor UI reads/writes it).
  - ``BRAND.md``   — a human- and agent-readable guide, regenerated on every save.

On every save we also (re)write a ``brand-guidelines`` skill so the agent can
pull the full guide on demand via ``use_skill`` (progressive disclosure).

This module is the single write path used by BOTH the HTTP API
(``/api/brand``) and the agent tools (``read_brand_guide`` / ``write_brand_guide``),
so the three artifacts (JSON, MD, skill) can never drift apart no matter which
surface made the change. Reads for prompt injection live in
``app.agents.leonardo.project_context`` and pick up these files directly.
"""

import os
import json
import copy
import logging

logger = logging.getLogger(__name__)

BRAND_DIR = ".leonardo"
BRAND_JSON_PATH = ".leonardo/brand.json"
BRAND_MD_PATH = ".leonardo/BRAND.md"
BRAND_SKILL_SLUG = "brand-guidelines"

DEFAULT_BRAND = {
    "colors": [
        {"name": "Primary", "hex": "#8B5CF6"},
        {"name": "Secondary", "hex": "#6366F1"},
        {"name": "Tertiary", "hex": "#06B6D4"},
    ],
    "logos": [],
    "notes": "",
}


def normalize_brand(brand: dict) -> dict:
    """Coerce arbitrary input into the canonical brand shape.

    Guarantees ``{"colors": [{"name","hex"}], "logos": [{"name","path"}],
    "notes": str}`` with every field a trimmed string. Tolerant of missing keys
    and junk entries (skips non-dict list items) so tool/LLM input can't corrupt
    the file.
    """
    brand = brand or {}

    colors = []
    for c in (brand.get("colors") or []):
        if isinstance(c, dict):
            colors.append({
                "name": str(c.get("name", "") or "").strip(),
                "hex": str(c.get("hex", "") or "").strip(),
            })

    logos = []
    for lg in (brand.get("logos") or []):
        if isinstance(lg, dict):
            logos.append({
                "name": str(lg.get("name", "") or "").strip(),
                "path": str(lg.get("path", "") or "").strip(),
            })

    return {
        "colors": colors,
        "logos": logos,
        "notes": str(brand.get("notes", "") or ""),
    }


def render_brand_md(brand: dict) -> str:
    """Render brand.json into a readable BRAND.md guide."""
    lines = [
        "# Brand Guide",
        "",
        "> Auto-generated from the Brand Guide editor in Leonardo. To change it,",
        "> edit the brand guide from the chat toolbar (or ask Leo) — hand edits",
        "> here are overwritten the next time the guide is saved.",
        "",
        "## Colors",
        "",
    ]

    colors = brand.get("colors") or []
    if colors:
        lines.append("| Name | Hex |")
        lines.append("| --- | --- |")
        for c in colors:
            name = (c.get("name") or "").strip() or "(unnamed)"
            hex_val = (c.get("hex") or "").strip()
            lines.append(f"| {name} | `{hex_val}` |")
    else:
        lines.append("_No colors defined yet._")
    lines.append("")

    lines.append("## Logos & Icons")
    lines.append("")
    logos = brand.get("logos") or []
    if logos:
        for lg in logos:
            name = (lg.get("name") or "").strip() or "(unnamed)"
            path = (lg.get("path") or "").strip()
            lines.append(f"- **{name}** — `{path}`")
    else:
        lines.append("_No logos added yet._")
    lines.append("")

    notes = (brand.get("notes") or "").strip()
    lines.append("## Notes")
    lines.append("")
    lines.append(notes if notes else "_No notes yet._")
    lines.append("")

    return "\n".join(lines)


def load_brand() -> dict:
    """Return the current brand guide (normalized), or a copy of the defaults."""
    if not os.path.exists(BRAND_JSON_PATH):
        return copy.deepcopy(DEFAULT_BRAND)
    try:
        with open(BRAND_JSON_PATH, "r", encoding="utf-8") as f:
            return normalize_brand(json.load(f))
    except Exception as e:
        logger.warning(f"Error reading brand.json, using defaults: {e}")
        return copy.deepcopy(DEFAULT_BRAND)


def brand_exists() -> bool:
    return os.path.exists(BRAND_JSON_PATH)


def _update_brand_skill(guide_md: str) -> None:
    """Mirror the full guide into the brand-guidelines skill (progressive
    disclosure). Non-fatal: the JSON/MD source of truth are already saved."""
    try:
        from app.agents.leonardo.skills import write_skill_file, MAX_SKILL_CONTENT_CHARS
        write_skill_file(
            name="Brand Guidelines",
            description=(
                "The project's official brand guide — brand colors (hex), logos, "
                "and style notes. Consult before any visual, design, theming, or "
                "styling change so the UI stays on-brand."
            ),
            content=guide_md[:MAX_SKILL_CONTENT_CHARS],
            slug=BRAND_SKILL_SLUG,
        )
    except Exception as e:
        logger.warning(f"Brand guide saved but brand-guidelines skill update failed: {e}")


def save_brand(brand: dict) -> dict:
    """Persist the brand guide: write brand.json + BRAND.md + the skill.

    Returns the normalized brand dict that was written. Raises on I/O failure of
    the two source-of-truth files (JSON/MD); the skill mirror is best-effort.
    """
    brand = normalize_brand(brand)
    os.makedirs(BRAND_DIR, exist_ok=True)

    guide_md = render_brand_md(brand)
    with open(BRAND_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(brand, f, indent=2, ensure_ascii=False)
    with open(BRAND_MD_PATH, "w", encoding="utf-8") as f:
        f.write(guide_md)

    _update_brand_skill(guide_md)
    return brand
