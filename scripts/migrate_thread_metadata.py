#!/usr/bin/env python3
"""One-time migration to populate ThreadMetadata from existing LangGraph checkpoints.

Run this after deploying the ThreadMetadata table to backfill existing threads.

Usage:
    cd /app  # or project root
    python scripts/migrate_thread_metadata.py

Or via Docker:
    docker compose exec llamabot python scripts/migrate_thread_metadata.py
"""
import os
import sys
import asyncio
from datetime import datetime, timezone

# Add app directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


async def migrate_existing_threads():
    """Migrate existing checkpoint threads to ThreadMetadata table."""
    from sqlmodel import Session
    from psycopg import Connection
    from langgraph.checkpoint.postgres import PostgresSaver

    from app.db import engine
    from app.models import ThreadMetadata
    from app.services.thread_service import extract_title_from_message

    if engine is None:
        print("ERROR: Database engine not available (LEONARDO_DB_URI not set)")
        return

    db_uri = os.getenv("DB_URI")
    if not db_uri:
        print("ERROR: DB_URI not set - cannot access LangGraph checkpoints")
        return

    print(f"Connecting to LangGraph checkpoint database...")

    # Connect to LangGraph checkpoint database
    conn = Connection.connect(db_uri, autocommit=True)
    checkpointer = PostgresSaver(conn)

    # Import workflow to access state
    print("Loading LangGraph workflow...")
    from app.agents.llamabot.nodes import build_workflow
    graph = build_workflow(checkpointer=checkpointer)

    migrated = 0
    skipped = 0
    errors = 0

    seen_threads = set()

    print("Starting migration...")
    print("=" * 50)

    # Iterate through all checkpoints (limit high to get all)
    async for checkpoint in checkpointer.alist(config={}, limit=10000):
        thread_id = checkpoint.config["configurable"]["thread_id"]

        if thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)

        # Check if already migrated
        with Session(engine) as session:
            existing = session.get(ThreadMetadata, thread_id)
            if existing:
                skipped += 1
                continue

        try:
            # Get full state to extract title
            config = {"configurable": {"thread_id": thread_id}}
            state = await graph.aget_state(config=config)

            # Handle state format - can be tuple or StateSnapshot
            if hasattr(state, 'values'):
                # StateSnapshot object
                messages = state.values.get('messages', [])
            elif isinstance(state, tuple) and len(state) > 0:
                # Tuple format
                messages = state[0].get('messages', []) if isinstance(state[0], dict) else []
            else:
                messages = []

            # Find first human message
            first_human_msg = None
            message_count = len(messages)

            for msg in messages:
                msg_type = getattr(msg, 'type', None) or (msg.get('type') if isinstance(msg, dict) else None)
                if msg_type == 'human':
                    first_human_msg = getattr(msg, 'content', None) or (msg.get('content') if isinstance(msg, dict) else None)
                    break

            title = extract_title_from_message(first_human_msg) if first_human_msg else "New Conversation"

            # Get timestamp from checkpoint metadata
            created_at = datetime.now(timezone.utc)
            if checkpoint.metadata and checkpoint.metadata.get('created_at'):
                try:
                    ts = checkpoint.metadata['created_at']
                    if isinstance(ts, str):
                        created_at = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    elif isinstance(ts, datetime):
                        created_at = ts
                except Exception:
                    pass

            # Create metadata entry
            with Session(engine) as session:
                metadata = ThreadMetadata(
                    thread_id=thread_id,
                    title=title,
                    created_at=created_at,
                    updated_at=created_at,
                    message_count=message_count
                )
                session.add(metadata)
                session.commit()

            migrated += 1
            print(f"  ✓ {thread_id[:30]}... ({message_count} msgs) - {title[:40]}")

            if migrated % 10 == 0:
                print(f"  ... migrated {migrated} threads so far")

        except Exception as e:
            print(f"  ✗ Error migrating thread {thread_id}: {e}")
            errors += 1

    conn.close()

    print("=" * 50)
    print(f"Migration complete!")
    print(f"  Migrated: {migrated}")
    print(f"  Skipped (already exists): {skipped}")
    print(f"  Errors: {errors}")
    print(f"  Total unique threads found: {len(seen_threads)}")


if __name__ == "__main__":
    print("Thread Metadata Migration Script")
    print("=" * 50)
    asyncio.run(migrate_existing_threads())
