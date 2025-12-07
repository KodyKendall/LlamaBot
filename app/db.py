import os
import logging
from sqlmodel import SQLModel, create_engine, Session

logger = logging.getLogger(__name__)

# Use AUTH_DB_URI for user auth database, separate from LangGraph's DB_URI
# This must be set in the environment (e.g., .env file)
DATABASE_URL = os.getenv("AUTH_DB_URI")

if not DATABASE_URL:
    logger.warning("AUTH_DB_URI is not set - auth database features will not work")
    engine = None
else:
    engine = create_engine(DATABASE_URL, echo=False)


def init_db():
    """Initialize database and create all tables."""
    if engine is None:
        logger.error("Cannot initialize auth database: AUTH_DB_URI is not set")
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
        raise RuntimeError("AUTH_DB_URI is not set - cannot create database session")
    with Session(engine) as session:
        yield session
