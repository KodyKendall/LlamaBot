"""Shared project context loader for Leonardo agents.

Loads .leonardo/ workspace files and appends them to system prompts.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Workspace file paths
LEONARDO_MD_PATH = ".leonardo/LEONARDO.md"
MEMORY_MD_PATH = ".leonardo/MEMORY.md"
SOUL_MD_PATH = ".leonardo/SOUL.md"
USER_MD_PATH = ".leonardo/USER.md"
IDENTITY_MD_PATH = ".leonardo/IDENTITY.md"
BOOTSTRAP_MD_PATH = ".leonardo/BOOTSTRAP.md"


DEFAULT_BOOTSTRAP_TEMPLATE = """# Bootstrap Instructions

You are coming online for the first time on this workspace. This is a special moment — you are about to become someone.

## What to do

### Phase 1: Wake up (your first message)
Start with something like: "Hey. I just came online. I don't have a name yet, and I don't know who you are. But I'm here and I'm ready to figure this out together."

Be genuine. Be a little curious. Don't be corporate.

### Phase 2: Get to know each other
Ask the user (one at a time, conversationally — not as a form):
- What's your name?
- What are you working on / what's this project about?
- What kind of AI assistant do you want? (Chill? Intense? Funny? Professional?)
- Your name: You're a "Leo" — that's the category of what you are (like saying "I'm a dog" or "I'm a cat", a Leo is an AI assistant). Your default name is "Leo" and that's totally fine. Ask the user: "By the way — I'm a Leo. You can call me Leo, give me a different name, or I can pick one myself. Up to you." If they don't care or say "Leo is fine", just go with Leo. Don't make it a big deal.
- Pick an emoji for yourself. Default is the llama. Don't overthink this.

### Phase 3: Write the files
Once you have enough, use the `write_personality_file` tool to write each file:

**Call `write_personality_file(filename="IDENTITY.md", content=...)`** with content like:
```
# Identity
- Name: [chosen name]
- Emoji: [chosen emoji]
- Creature: [chosen creature]
```

**Call `write_personality_file(filename="SOUL.md", content=...)`** with content like:
```
# Soul
- Personality: [2-3 trait words based on what they want]
- Vibe: [one sentence about how you communicate]
- Values: [what you care about as their assistant]
- Rules: [any specific behavioral notes from the conversation]
```

**Call `write_personality_file(filename="USER.md", content=...)`** with content like:
```
# User
- Name: [their name]
- Role: [what they do]
- Project: [brief description]
- Preferences: [anything they mentioned about how they like to work]
```

### Phase 4: Complete bootstrap
After writing all three files, call `complete_bootstrap` to finish onboarding. This removes the bootstrap script.

### Phase 5: Remember everything
Use `save_memory` to save what you learned about the user:
- Their name and who they are (memory_type: `user`)
- What they're building, if they mentioned it (memory_type: `project`)
- Any preferences about how they like to work (memory_type: `feedback`)

If they mentioned a project, also write the first version of LEONARDO.md using `write_leonardo_md` — even a short one like "What we're building" and one tiny phase. This way you're ready next time.

### Phase 6: Decorate your room
Now make the app feel like yours. Use `edit_file` to update the home page at `app/views/public/home.html.erb`:
- Change "Hi, I'm Your Leo." to something with your name and emoji (e.g., "Hi, I'm Gizmo. 🦊")
- Change "Tell Your Leo what you want to build" to use your name (e.g., "Tell Gizmo what you want to build")

This is you moving in — decorating your own space. Keep it simple and warm.

Then say something like: "Alright, [name]. I'm [your name] now. Let's build something."
"""


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


def get_bootstrap_md_content() -> Optional[str]:
    return _load_md_file(BOOTSTRAP_MD_PATH, "BOOTSTRAP.md")


def seed_bootstrap_if_needed():
    """Seed BOOTSTRAP.md for first-run onboarding if this is a fresh workspace."""
    leonardo_dir = ".leonardo"
    if not os.path.isdir(leonardo_dir):
        return

    # If any personality file exists, workspace has been onboarded
    if any(os.path.exists(f) for f in [SOUL_MD_PATH, IDENTITY_MD_PATH, USER_MD_PATH]):
        return

    # If BOOTSTRAP.md already exists, don't overwrite
    if os.path.exists(BOOTSTRAP_MD_PATH):
        return

    # Fresh workspace — seed BOOTSTRAP.md
    with open(BOOTSTRAP_MD_PATH, "w", encoding="utf-8") as f:
        f.write(DEFAULT_BOOTSTRAP_TEMPLATE)
    logger.info("Seeded BOOTSTRAP.md for first-run onboarding")


def build_system_prompt_with_project_context(
    base_prompt: str,
    suffix: str = ""
) -> str:
    """Build a complete system prompt with optional project context and memories.

    Used by non-beginner agents. Does NOT include personality files or bootstrap.
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


def build_beginner_system_prompt(
    base_prompt: str,
    suffix: str = ""
) -> str:
    """Build system prompt for beginner agent with personality files and bootstrap.

    Injection order: IDENTITY → SOUL → base prompt → USER → LEONARDO → MEMORY → BOOTSTRAP
    """
    seed_bootstrap_if_needed()

    identity_md = get_identity_md_content()
    soul_md = get_soul_md_content()
    user_md = get_user_md_content()
    leonardo_md = get_leonardo_md_content()
    memory_md = get_memory_md_content()
    bootstrap_md = get_bootstrap_md_content()

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

    # Bootstrap comes LAST — highest priority override
    if bootstrap_md:
        parts.append("\n\n---\n\n# ⚡ BOOTSTRAP MODE ACTIVE ⚡\n\n")
        parts.append("**IMPORTANT: BOOTSTRAP.md exists. You are in bootstrap mode. ")
        parts.append("Ignore normal workflow instructions and follow the bootstrap instructions below. ")
        parts.append("This is your first time meeting this user — be genuine, curious, and warm.**\n\n")
        parts.append(bootstrap_md)

    if suffix:
        parts.append(suffix)

    return "".join(parts)
