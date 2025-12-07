"""Migration service for auth.json to SQLite."""
import json
import logging
from pathlib import Path
from sqlmodel import Session
from app.models import User
from app.services.user_service import get_user_by_username

logger = logging.getLogger(__name__)


def migrate_auth_json(session: Session, auth_file: Path) -> bool:
    """
    Migrate user from auth.json to SQLite database.

    Returns True if migration was successful or not needed.
    """
    if not auth_file.exists():
        logger.info("No auth.json file found, no migration needed.")
        return True

    try:
        with open(auth_file, 'r') as f:
            auth_data = json.load(f)

        username = auth_data.get("username")
        password_hash = auth_data.get("password_hash")

        if not username or not password_hash:
            logger.warning("auth.json is missing required fields.")
            return False

        # Check if user already exists in database
        existing_user = get_user_by_username(session, username)
        if existing_user:
            logger.info(f"User '{username}' already exists in database, skipping migration.")
        else:
            # Create user with existing password hash (don't re-hash)
            # First migrated user becomes admin
            user = User(
                username=username,
                password_hash=password_hash,
                is_admin=True  # First user from auth.json is admin
            )
            session.add(user)
            session.commit()
            logger.info(f"Successfully migrated user '{username}' to database as admin.")

        # Rename auth.json to indicate migration complete
        migrated_file = auth_file.with_suffix('.json.migrated')
        auth_file.rename(migrated_file)
        logger.info(f"Renamed {auth_file} to {migrated_file}")

        return True

    except json.JSONDecodeError as e:
        logger.error(f"Error parsing auth.json: {e}")
        return False
    except Exception as e:
        logger.error(f"Error during migration: {e}")
        return False
