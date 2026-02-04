"""Prompt library service for CRUD operations."""
import logging
from datetime import datetime, timezone
from typing import Optional, List
from sqlmodel import Session, select

from app.models import Prompt

logger = logging.getLogger(__name__)


def create_prompt(
    session: Session,
    name: str,
    content: str,
    group: str = "General",
    description: Optional[str] = None
) -> Prompt:
    """Create a new prompt in the library."""
    prompt = Prompt(
        name=name.strip(),
        content=content,
        group=group.strip(),
        description=description.strip() if description else None
    )
    session.add(prompt)
    session.commit()
    session.refresh(prompt)
    logger.info(f"Created prompt: {prompt.name} (id={prompt.id})")
    return prompt


def get_prompt_by_id(session: Session, prompt_id: int) -> Optional[Prompt]:
    """Get a prompt by ID."""
    return session.get(Prompt, prompt_id)


def get_all_prompts(
    session: Session,
    include_inactive: bool = False
) -> List[Prompt]:
    """Get all prompts, optionally including inactive ones."""
    stmt = select(Prompt).order_by(Prompt.group, Prompt.name)
    if not include_inactive:
        stmt = stmt.where(Prompt.is_active == True)  # noqa: E712
    return list(session.exec(stmt).all())


def get_prompts_by_group(
    session: Session,
    group: str
) -> List[Prompt]:
    """Get all active prompts in a specific group."""
    stmt = select(Prompt).where(
        Prompt.group == group,
        Prompt.is_active == True  # noqa: E712
    ).order_by(Prompt.name)
    return list(session.exec(stmt).all())


def get_prompt_groups(session: Session) -> List[str]:
    """Get list of unique prompt groups."""
    stmt = select(Prompt.group).where(Prompt.is_active == True).distinct()  # noqa: E712
    return sorted(list(session.exec(stmt).all()))


def update_prompt(
    session: Session,
    prompt_id: int,
    name: Optional[str] = None,
    content: Optional[str] = None,
    group: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None
) -> Optional[Prompt]:
    """Update a prompt's attributes."""
    prompt = get_prompt_by_id(session, prompt_id)
    if not prompt:
        return None

    if name is not None:
        prompt.name = name.strip()
    if content is not None:
        prompt.content = content
    if group is not None:
        prompt.group = group.strip()
    if description is not None:
        prompt.description = description.strip() if description else None
    if is_active is not None:
        prompt.is_active = is_active

    prompt.updated_at = datetime.now(timezone.utc)
    session.add(prompt)
    session.commit()
    session.refresh(prompt)
    logger.info(f"Updated prompt: {prompt.name} (id={prompt.id})")
    return prompt


def delete_prompt(session: Session, prompt_id: int, hard_delete: bool = False) -> bool:
    """Delete a prompt (soft delete by default)."""
    prompt = get_prompt_by_id(session, prompt_id)
    if not prompt:
        return False

    if hard_delete:
        session.delete(prompt)
        logger.info(f"Hard deleted prompt id={prompt_id}")
    else:
        prompt.is_active = False
        prompt.updated_at = datetime.now(timezone.utc)
        session.add(prompt)
        logger.info(f"Soft deleted prompt: {prompt.name} (id={prompt_id})")

    session.commit()
    return True


def increment_usage(session: Session, prompt_id: int) -> Optional[Prompt]:
    """Increment the usage count for a prompt."""
    prompt = get_prompt_by_id(session, prompt_id)
    if not prompt:
        return None

    prompt.usage_count += 1
    prompt.updated_at = datetime.now(timezone.utc)
    session.add(prompt)
    session.commit()
    session.refresh(prompt)
    return prompt


def search_prompts(
    session: Session,
    query: str
) -> List[Prompt]:
    """Search prompts by name or content."""
    search_term = f"%{query.lower()}%"
    stmt = select(Prompt).where(
        Prompt.is_active == True,  # noqa: E712
        (Prompt.name.ilike(search_term)) | (Prompt.content.ilike(search_term))
    ).order_by(Prompt.usage_count.desc())
    return list(session.exec(stmt).all())
