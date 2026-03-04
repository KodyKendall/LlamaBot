# init_pg_checkpointer.py
import os
from urllib.parse import urlparse, urlunparse
from langgraph.checkpoint.postgres import PostgresSaver, ConnectionPool
from dotenv import load_dotenv
from psycopg import Connection

#https://github.com/langchain-ai/langgraph/issues/2887

load_dotenv()

# Checkpointer DB: defaults to llamabot_production (same as auth DB) for consolidated storage
# Falls back through multiple env vars for backwards compatibility
checkpointer_db_uri = (
    os.getenv("CHECKPOINTER_DB_URI") or
    os.getenv("LEONARDO_DB_URI") or
    os.getenv("AUTH_DB_URI") or
    os.getenv("DB_URI")  # Legacy fallback
)
llamabot_db_uri = os.getenv("LLAMABOT_DB_URI") or os.getenv("LEONARDO_DB_URI") or os.getenv("AUTH_DB_URI")  # Backwards compat


def ensure_env_variable(key: str, default_value: str, env_file: str = ".env") -> str:
    """Ensure an environment variable exists in .env file, create if missing."""
    from pathlib import Path

    env_path = Path(env_file)

    # Check if variable exists in environment
    current_value = os.getenv(key)
    if current_value:
        return current_value

    # Variable doesn't exist, add it to .env
    print(f"🔧 {key} not found, adding to {env_file}...")

    # Read existing .env content
    env_content = ""
    if env_path.exists():
        env_content = env_path.read_text()

    # Add the new variable
    if env_content and not env_content.endswith('\n'):
        env_content += '\n'
    env_content += f'{key}="{default_value}"\n'

    # Write back to .env
    env_path.write_text(env_content)

    # Set in current environment
    os.environ[key] = default_value

    print(f"✅ Added {key} to {env_file}")
    return default_value


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


def run_alembic_migrations(max_retries: int = 3, retry_delay: float = 2.0):
    """Run Alembic migrations to upgrade the auth database schema."""
    import time
    from pathlib import Path
    from alembic.config import Config
    from alembic import command

    # Get the path to alembic.ini relative to this file
    app_dir = Path(__file__).parent
    alembic_ini = app_dir / "alembic.ini"

    if not alembic_ini.exists():
        print(f"⚠️ Alembic config not found at {alembic_ini}")
        return

    # Create Alembic config
    alembic_cfg = Config(str(alembic_ini))

    # Retry logic for database connection issues
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 Running Alembic migrations (attempt {attempt}/{max_retries})...")
            command.upgrade(alembic_cfg, "head")
            print("✅ Alembic migrations completed")
            return  # Success, exit the function
        except Exception as e:
            error_msg = str(e)
            if attempt < max_retries and "password authentication failed" in error_msg:
                print(f"⚠️ Migration attempt {attempt} failed, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                print(f"⚠️ Alembic migration error: {e}")
                # Don't raise - let the app continue even if migrations fail
                return


def test_sqlalchemy_connection(uri: str) -> bool:
    """Test if SQLAlchemy/psycopg2 can connect to the database."""
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(uri)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        print(f"✅ SQLAlchemy connection test passed")
        return True
    except Exception as e:
        print(f"⚠️ SQLAlchemy connection test failed: {e}")
        return False


# Initialize LlamaBot database if LLAMABOT_DB_URI is set
if llamabot_db_uri is None:
    print("LLAMABOT_DB_URI/LEONARDO_DB_URI/AUTH_DB_URI is not set, skipping LlamaBot database initialization")
else:
    print(f"LLAMABOT_DB_URI/LEONARDO_DB_URI is set, initializing LlamaBot database...")
    if ensure_database_exists(llamabot_db_uri):
        # Test SQLAlchemy connection before running migrations
        if test_sqlalchemy_connection(llamabot_db_uri):
            run_alembic_migrations()
        else:
            print("⚠️ Skipping migrations - SQLAlchemy cannot connect")

# Initialize LangGraph checkpointer if checkpointer_db_uri is set
if checkpointer_db_uri is None:
    print("CHECKPOINTER_DB_URI/LEONARDO_DB_URI/AUTH_DB_URI/DB_URI is not set, we'll use InMemoryCheckpointer instead!")
else:
    print("Checkpointer DB URI is set, we'll use PostgresSaver instead!")
    print("Checkpointer DB URI: ", checkpointer_db_uri)

    # Ensure the database exists before connecting
    if not ensure_database_exists(checkpointer_db_uri):
        print("⚠️ Could not ensure checkpointer database exists, continuing anyway...")

    # Create connection pool
    # pool = ConnectionPool(checkpointer_db_uri)
    conn = Connection.connect(checkpointer_db_uri, autocommit=True)

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
