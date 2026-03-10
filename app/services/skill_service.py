"""Skill library service for CRUD operations."""
import logging
from datetime import datetime, timezone
from typing import Optional, List
from sqlmodel import Session, select

from app.models import Skill

logger = logging.getLogger(__name__)


def create_skill(
    session: Session,
    name: str,
    content: str,
    group: str = "General",
    description: Optional[str] = None
) -> Skill:
    """Create a new skill in the library."""
    skill = Skill(
        name=name.strip(),
        content=content,
        group=group.strip(),
        description=description.strip() if description else None
    )
    session.add(skill)
    session.commit()
    session.refresh(skill)
    logger.info(f"Created skill: {skill.name} (id={skill.id})")
    return skill


def get_skill_by_id(session: Session, skill_id: int) -> Optional[Skill]:
    """Get a skill by ID."""
    return session.get(Skill, skill_id)


def get_all_skills(
    session: Session,
    include_inactive: bool = False
) -> List[Skill]:
    """Get all skills, optionally including inactive ones."""
    stmt = select(Skill).order_by(Skill.group, Skill.name)
    if not include_inactive:
        stmt = stmt.where(Skill.is_active == True)  # noqa: E712
    return list(session.exec(stmt).all())


def get_skills_by_group(
    session: Session,
    group: str
) -> List[Skill]:
    """Get all active skills in a specific group."""
    stmt = select(Skill).where(
        Skill.group == group,
        Skill.is_active == True  # noqa: E712
    ).order_by(Skill.name)
    return list(session.exec(stmt).all())


def get_skill_groups(session: Session) -> List[str]:
    """Get list of unique skill groups."""
    stmt = select(Skill.group).where(Skill.is_active == True).distinct()  # noqa: E712
    return sorted(list(session.exec(stmt).all()))


def update_skill(
    session: Session,
    skill_id: int,
    name: Optional[str] = None,
    content: Optional[str] = None,
    group: Optional[str] = None,
    description: Optional[str] = None,
    is_active: Optional[bool] = None
) -> Optional[Skill]:
    """Update a skill's attributes."""
    skill = get_skill_by_id(session, skill_id)
    if not skill:
        return None

    if name is not None:
        skill.name = name.strip()
    if content is not None:
        skill.content = content
    if group is not None:
        skill.group = group.strip()
    if description is not None:
        skill.description = description.strip() if description else None
    if is_active is not None:
        skill.is_active = is_active

    skill.updated_at = datetime.now(timezone.utc)
    session.add(skill)
    session.commit()
    session.refresh(skill)
    logger.info(f"Updated skill: {skill.name} (id={skill.id})")
    return skill


def delete_skill(session: Session, skill_id: int, hard_delete: bool = False) -> bool:
    """Delete a skill (soft delete by default)."""
    skill = get_skill_by_id(session, skill_id)
    if not skill:
        return False

    if hard_delete:
        session.delete(skill)
        logger.info(f"Hard deleted skill id={skill_id}")
    else:
        skill.is_active = False
        skill.updated_at = datetime.now(timezone.utc)
        session.add(skill)
        logger.info(f"Soft deleted skill: {skill.name} (id={skill_id})")

    session.commit()
    return True


def increment_usage(session: Session, skill_id: int) -> Optional[Skill]:
    """Increment the usage count for a skill."""
    skill = get_skill_by_id(session, skill_id)
    if not skill:
        return None

    skill.usage_count += 1
    skill.updated_at = datetime.now(timezone.utc)
    session.add(skill)
    session.commit()
    session.refresh(skill)
    return skill


def search_skills(
    session: Session,
    query: str
) -> List[Skill]:
    """Search skills by name or content."""
    search_term = f"%{query.lower()}%"
    stmt = select(Skill).where(
        Skill.is_active == True,  # noqa: E712
        (Skill.name.ilike(search_term)) | (Skill.content.ilike(search_term))
    ).order_by(Skill.usage_count.desc())
    return list(session.exec(stmt).all())


def seed_default_skills(session: Session) -> int:
    """Seed default skills into the database if they don't exist.

    Returns the number of skills created.
    """
    from app.services.default_skills import DEFAULT_SKILLS

    created_count = 0
    for skill_data in DEFAULT_SKILLS:
        # Check if skill with this name already exists
        stmt = select(Skill).where(Skill.name == skill_data["name"])
        existing = session.exec(stmt).first()

        if existing is None:
            skill = Skill(
                name=skill_data["name"],
                content=skill_data["content"],
                group=skill_data.get("group", "General"),
                description=skill_data.get("description")
            )
            session.add(skill)
            created_count += 1
            logger.info(f"Seeded default skill: {skill.name}")

    if created_count > 0:
        session.commit()
        logger.info(f"Seeded {created_count} default skills")

    return created_count
