import os
import logging
from sqlmodel import SQLModel, create_engine, Session

logger = logging.getLogger(__name__)

# Use LEONARDO_DB_URI for user auth database, separate from LangGraph's DB_URI
# Falls back to AUTH_DB_URI for backwards compatibility
DATABASE_URL = os.getenv("LEONARDO_DB_URI") or os.getenv("AUTH_DB_URI")  # Backwards compat

# Debug: print what we're getting
print(f"DEBUG db.py: LEONARDO_DB_URI/AUTH_DB_URI = {DATABASE_URL}")

if not DATABASE_URL:
    logger.warning("LEONARDO_DB_URI/AUTH_DB_URI is not set - auth database features will not work")
    engine = None
else:
    print(f"DEBUG db.py: Creating engine with URL: {DATABASE_URL}")
    engine = create_engine(DATABASE_URL, echo=False)


def init_db():
    """Initialize database and create all tables."""
    if engine is None:
        logger.error("Cannot initialize auth database: LEONARDO_DB_URI/AUTH_DB_URI is not set")
        return

    # Import models to register them with SQLModel
    from app.models import User  # noqa: F401
    try:
        SQLModel.metadata.create_all(engine)
        logger.info("✅ Auth database initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize auth database: {e}")
        raise


def get_session():
    """Dependency for getting database sessions."""
    if engine is None:
        raise RuntimeError("LEONARDO_DB_URI/AUTH_DB_URI is not set - cannot create database session")
    with Session(engine) as session:
        yield session
