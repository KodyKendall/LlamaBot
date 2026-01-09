"""Thread metadata service for lazy loading optimization."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List
from sqlmodel import Session, select

from app.models import ThreadMetadata

logger = logging.getLogger(__name__)


def extract_title_from_message(message_content, max_length: int = 50) -> str:
    """Extract a title from the first user message content.

    Handles various content formats from LangGraph messages:
    - String content
    - List of content blocks (multimodal messages)
    - Dict with text field
    """
    if not message_content:
        return "New Conversation"

    # Handle different content formats
    if isinstance(message_content, list):
        # Extract text from content blocks
        text_parts = []
        for block in message_content:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict):
                if block.get('type') == 'text' and block.get('text'):
                    text_parts.append(block['text'])
                elif block.get('text'):
                    text_parts.append(block['text'])
        message_content = ' '.join(text_parts)
    elif isinstance(message_content, dict):
        message_content = message_content.get('text', '') or message_content.get('content', '')

    # Ensure it's a string
    if not isinstance(message_content, str):
        message_content = str(message_content)

    # Truncate and add ellipsis if needed
    title = message_content[:max_length].strip()
    if len(message_content) > max_length:
        title += '...'

    return title or "New Conversation"


async def generate_title_with_llm(message_content: str, timeout_seconds: float = 10.0) -> Optional[str]:
    """Generate a descriptive title using GPT-5-mini.

    This function is designed to be completely safe - it will never raise an exception.
    On any failure, it returns None and the caller should fall back to the truncated title.

    Args:
        message_content: The first user message content
        timeout_seconds: Maximum time to wait for LLM response

    Returns:
        Generated title string, or None if generation fails for any reason
    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:
        logger.debug(f"langchain_openai not available: {e}")
        return None

    try:
        # Extract text content if needed (handle various formats)
        text_content = message_content
        if isinstance(message_content, list):
            text_parts = []
            for block in message_content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict):
                    if block.get('type') == 'text' and block.get('text'):
                        text_parts.append(block['text'])
                    elif block.get('text'):
                        text_parts.append(block['text'])
            text_content = ' '.join(text_parts)
        elif isinstance(message_content, dict):
            text_content = message_content.get('text', '') or message_content.get('content', '')

        if not text_content or not isinstance(text_content, str):
            return None

        # Skip if message is too short to need summarization
        if len(text_content.strip()) < 10:
            return None

        # Truncate very long messages to avoid unnecessary token usage
        text_content = text_content[:500]

        model = ChatOpenAI(
            model="gpt-5-mini",
            temperature=0.7,
            max_tokens=50,
            model_kwargs={"reasoning_effort": "low"},
            request_timeout=timeout_seconds,
        )

        prompt = f"""Generate a concise 3-6 word title summarizing this conversation topic.
Return ONLY the title text, no quotes, no punctuation at the end.

User message: {text_content}"""

        # Use asyncio.wait_for for additional timeout protection
        response = await asyncio.wait_for(
            model.ainvoke(prompt),
            timeout=timeout_seconds
        )

        # Safely extract content
        if not response or not hasattr(response, 'content') or not response.content:
            return None

        title = response.content.strip().strip('"\'')

        # Validate the title is reasonable
        if not title or len(title) < 2:
            return None

        # Ensure title isn't too long
        if len(title) > 100:
            title = title[:97] + '...'

        logger.info(f"Generated LLM title: {title}")
        return title

    except asyncio.TimeoutError:
        logger.debug("LLM title generation timed out")
        return None
    except Exception as e:
        # Catch absolutely everything - this should never propagate errors
        logger.debug(f"LLM title generation failed (non-critical): {e}")
        return None


async def schedule_title_generation(thread_id: str, message_content: str):
    """Schedule async title generation and update thread metadata when complete.

    This runs in the background without blocking the main request.
    Designed to be completely safe - will never raise exceptions or affect the main flow.
    If title generation fails, the original truncated title remains unchanged.
    """
    try:
        # Validate inputs
        if not thread_id or not message_content:
            return

        title = await generate_title_with_llm(message_content)

        # Only update if we got a valid title
        if not title:
            logger.debug(f"No LLM title generated for {thread_id}, keeping truncated title")
            return

        # Import here to avoid circular imports
        try:
            from app.db import engine
        except ImportError:
            logger.debug("Database module not available for title update")
            return

        if engine is None:
            logger.debug("Database engine not available for title update")
            return

        try:
            with Session(engine) as session:
                update_thread_metadata(session, thread_id, new_title=title)
                logger.info(f"Updated thread {thread_id} with LLM-generated title: {title}")
        except Exception as db_error:
            # Database errors should not propagate
            logger.debug(f"Database update failed for title (non-critical): {db_error}")

    except Exception as e:
        # Catch-all: this function must never raise
        logger.debug(f"Background title generation failed (non-critical): {e}")


def get_or_create_thread_metadata(
    session: Session,
    thread_id: str,
    first_message_content: Optional[str] = None,
    user_id: Optional[int] = None,
    agent_name: Optional[str] = None
) -> ThreadMetadata:
    """Get existing metadata or create new entry for a thread."""
    metadata = session.get(ThreadMetadata, thread_id)

    if metadata is None:
        title = extract_title_from_message(first_message_content) if first_message_content else "New Conversation"
        metadata = ThreadMetadata(
            thread_id=thread_id,
            title=title,
            message_count=1 if first_message_content else 0,
            user_id=user_id,
            agent_name=agent_name
        )
        session.add(metadata)
        session.commit()
        session.refresh(metadata)

    return metadata


def update_thread_metadata(
    session: Session,
    thread_id: str,
    increment_messages: int = 0,
    new_title: Optional[str] = None
) -> Optional[ThreadMetadata]:
    """Update metadata when a message is added to a thread.

    Args:
        session: Database session
        thread_id: Thread ID to update
        increment_messages: Number to add to message_count (default 0)
        new_title: Optional new title to set

    Returns:
        Updated ThreadMetadata or None if not found
    """
    metadata = session.get(ThreadMetadata, thread_id)

    if metadata:
        metadata.updated_at = datetime.now(timezone.utc)
        if increment_messages > 0:
            metadata.message_count += increment_messages
        if new_title:
            metadata.title = new_title[:100]

        session.add(metadata)
        session.commit()
        session.refresh(metadata)

    return metadata


def get_thread_list(
    session: Session,
    user_id: Optional[int] = None,
    before: Optional[datetime] = None,
    limit: int = 10
) -> List[ThreadMetadata]:
    """Get thread metadata list with cursor-based pagination.

    Args:
        session: Database session
        user_id: Optional filter by user ID
        before: Optional cursor - return threads older than this timestamp
        limit: Maximum number of threads to return

    Returns:
        List of ThreadMetadata sorted by updated_at descending
    """
    stmt = select(ThreadMetadata).order_by(ThreadMetadata.updated_at.desc())

    if user_id is not None:
        stmt = stmt.where(ThreadMetadata.user_id == user_id)

    if before is not None:
        stmt = stmt.where(ThreadMetadata.updated_at < before)

    stmt = stmt.limit(limit)

    return list(session.exec(stmt).all())


def delete_thread_metadata(session: Session, thread_id: str) -> bool:
    """Delete thread metadata entry.

    Args:
        session: Database session
        thread_id: Thread ID to delete

    Returns:
        True if deleted, False if not found
    """
    metadata = session.get(ThreadMetadata, thread_id)
    if metadata:
        session.delete(metadata)
        session.commit()
        return True
    return False
