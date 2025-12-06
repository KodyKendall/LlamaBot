# init_pg_checkpointer.py
import os
from urllib.parse import urlparse, urlunparse
from langgraph.checkpoint.postgres import PostgresSaver, ConnectionPool
from dotenv import load_dotenv
from psycopg import Connection

#https://github.com/langchain-ai/langgraph/issues/2887

load_dotenv()

db_uri = os.getenv("DB_URI")
auth_db_uri = os.getenv("AUTH_DB_URI")


def get_db_name_from_uri(uri: str) -> str:
    """Extract database name from a PostgreSQL URI."""
    parsed = urlparse(uri)
    # Path is like '/llamabot' - strip the leading slash
    return parsed.path.lstrip('/')


def get_admin_uri_from_uri(uri: str) -> str:
    """Convert a database URI to connect to the 'postgres' admin database."""
    parsed = urlparse(uri)
    # Replace the database name (path) with 'postgres'
    admin_parsed = parsed._replace(path='/postgres')
    return urlunparse(admin_parsed)


def ensure_database_exists(uri: str) -> bool:
    """Create the database if it doesn't exist. Returns True if successful."""
    db_name = get_db_name_from_uri(uri)
    admin_uri = get_admin_uri_from_uri(uri)

    try:
        conn = Connection.connect(admin_uri, autocommit=True)
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (db_name,)
            )
            exists = cursor.fetchone()

            if not exists:
                cursor.execute(f'CREATE DATABASE {db_name}')
                print(f"✅ Created database '{db_name}'")
            else:
                print(f"✅ Database '{db_name}' already exists")
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Could not create database '{db_name}': {e}")
        return False


def run_alembic_migrations():
    """Run Alembic migrations to upgrade the auth database schema."""
    from pathlib import Path
    from alembic.config import Config
    from alembic import command

    try:
        # Get the path to alembic.ini relative to this file
        app_dir = Path(__file__).parent
        alembic_ini = app_dir / "alembic.ini"

        if not alembic_ini.exists():
            print(f"⚠️ Alembic config not found at {alembic_ini}")
            return

        # Create Alembic config
        alembic_cfg = Config(str(alembic_ini))

        # Run migrations
        print("🔄 Running Alembic migrations...")
        command.upgrade(alembic_cfg, "head")
        print("✅ Alembic migrations completed")

    except Exception as e:
        print(f"⚠️ Alembic migration error: {e}")
        # Don't raise - let the app continue even if migrations fail


# Initialize auth database if AUTH_DB_URI is set
if auth_db_uri is None:
    print("AUTH_DB_URI is not set, skipping auth database initialization")
else:
    print(f"AUTH_DB_URI is set, initializing auth database...")
    if ensure_database_exists(auth_db_uri):
        run_alembic_migrations()

# Initialize LangGraph checkpointer if DB_URI is set
if db_uri is None:
    print("DB_URI is not set, we'll use InMemoryCheckpointer instead!")
else:
    print("DB_URI is set, we'll use PostgresSaver instead!")
    print("DB_URI: ", db_uri)

    # Create connection pool
    # pool = ConnectionPool(db_uri)
    conn = Connection.connect(db_uri, autocommit=True)

    checkpointer_already_initialized = False
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
            ('checkpoints',),
        )
        table_exists = cursor.fetchone()[0]
        print(f"Table 'checkpoints' exists: {table_exists}")
        checkpointer_already_initialized = table_exists

    # Create the saver
    checkpointer = PostgresSaver(conn)

    # This runs DDL like CREATE TABLE and CREATE INDEX
    # including CREATE INDEX CONCURRENTLY, which must be run outside a transaction
    if not checkpointer_already_initialized:
        checkpointer.setup()

    print("✅ Checkpointer tables & indexes initialized.")
