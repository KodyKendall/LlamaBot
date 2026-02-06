# Load environment variables FIRST before any other imports that need them
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import os
import logging
import asyncio
import signal
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

from sqlmodel import Session
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.db import init_db, engine
from app.services.auth_migration import migrate_auth_json
from app.services.mothership_client import MothershipClient
from app.services.lease_manager import LeaseManager
from app.websocket.web_socket_connection_manager import WebSocketConnectionManager
from app.websocket.request_handler import RequestHandler

# Import routers
from app.routers import ui, api, websocket

# Configure logging to write info-level events to both chat_app.log and stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('chat_app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI()

# Add CORS middleware for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for ngrok/external access
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static directories
frontend_dir = Path(__file__).parent / "frontend"
app.mount("/frontend", StaticFiles(directory=str(frontend_dir)), name="frontend")

# This is responsible for holding and managing all active websocket connections.
manager = WebSocketConnectionManager(app)

# Application state to hold persistent checkpointer, important for session-based persistence.
app.state.checkpointer = None
app.state.async_checkpointer = None
app.state.timestamp = datetime.now(timezone.utc)
app.state.compiled_graphs = {}  # Cache for pre-compiled LangGraph workflows

# Mothership integration for lease management
app.state.mothership_client = MothershipClient()
app.state.lease_manager = LeaseManager(app, app.state.mothership_client)

# Path to legacy auth file (for migration)
LEGACY_AUTH_FILE = Path(__file__).parent / "auth.json"

# Suppress psycopg connection error spam when PostgreSQL is unavailable
psycopg_logger = logging.getLogger('psycopg.pool')
psycopg_logger.setLevel(logging.ERROR)


def get_or_create_checkpointer():
    """Get persistent checkpointer, creating once if needed"""
    if app.state.checkpointer is None:
        db_uri = os.getenv("DB_URI")
        if db_uri and db_uri.strip():
            try:
                # Create connection pool with limited retries and timeout
                # Reduced pool size for single worker (was max_size=5)
                pool = ConnectionPool(
                    db_uri,
                    min_size=1,
                    max_size=2,  # Reduced from 5 (single worker doesn't need many)
                    timeout=5.0,  # 5 second connection timeout
                    max_idle=300.0,  # 5 minute idle timeout
                    max_lifetime=3600.0,  # 1 hour max connection lifetime
                    reconnect_failed=lambda pool: logger.warning("PostgreSQL connection failed, using MemorySaver for persistence")
                )
                app.state.checkpointer = PostgresSaver(pool)
                # app.state.checkpointer.setup()
                logger.info("Connected to PostgreSQL for persistence")
            except Exception as e:
                logger.warning(f"DB_URI: {db_uri}")
                logger.warning(f"PostgreSQL unavailable ({str(e).split(':', 1)[0]}). Using MemorySaver for session-based persistence.")
                app.state.checkpointer = MemorySaver()
        else:
            logger.info("No DB_URI configured. Using MemorySaver for session-based persistence.")
            app.state.checkpointer = MemorySaver()

    return app.state.checkpointer


def get_or_create_async_checkpointer():
    """Get async persistent checkpointer, creating once if needed"""
    if app.state.async_checkpointer is None:
        db_uri = os.getenv("DB_URI")
        if db_uri and db_uri.strip():
            try:
                # Create async connection pool with limited retries and timeout
                # Reduced pool size for single worker (was max_size=5)
                pool = AsyncConnectionPool(
                    db_uri,
                    min_size=1,
                    max_size=2,  # Reduced from 5 (single worker doesn't need many)
                    timeout=5.0,  # 5 second connection timeout
                    max_idle=300.0,  # 5 minute idle timeout
                    max_lifetime=3600.0,  # 1 hour max connection lifetime
                    reconnect_failed=lambda pool: logger.warning("PostgreSQL async connection failed, using MemorySaver for persistence")
                )
                app.state.async_checkpointer = AsyncPostgresSaver(pool)
                # app.state.async_checkpointer.setup()
                logger.info("Connected to PostgreSQL (async) for persistence")
            except Exception as e:
                logger.warning(f"DB_URI: {db_uri}")
                logger.warning(f"PostgreSQL unavailable for async operations ({str(e).split(':', 1)[0]}). Using MemorySaver for session-based persistence.")
                app.state.async_checkpointer = MemorySaver()
        else:
            logger.info("No DB_URI configured. Using MemorySaver for async session-based persistence.")
            app.state.async_checkpointer = MemorySaver()

    return app.state.async_checkpointer


# Attach checkpointer helpers to app.state for access from routers
app.state.get_or_create_checkpointer = get_or_create_checkpointer
app.state.get_or_create_async_checkpointer = get_or_create_async_checkpointer


def get_langgraph_app_and_state_helper(message: dict):
    """Helper function to access RequestHandler.get_langgraph_app_and_state from main.py"""
    request_handler = RequestHandler(app)
    response = request_handler.get_langgraph_app_and_state(message)
    return response


# At module level
thread_locks = defaultdict(asyncio.Lock)
thread_queues = defaultdict(asyncio.Queue)
MAX_QUEUE_SIZE = 10


# Include routers
app.include_router(ui.router)
app.include_router(api.router)
app.include_router(websocket.router)


async def graceful_shutdown(sig):
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    logger.info(f"Received signal {sig.name}, initiating graceful shutdown...")

    # Stop lease manager
    if hasattr(app.state, 'lease_manager'):
        await app.state.lease_manager.stop()

    # Notify mothership of teardown
    if hasattr(app.state, 'mothership_client') and app.state.mothership_client.enabled:
        try:
            await app.state.mothership_client.notify_teardown(reason="sigterm")
            logger.info("Mothership notified of teardown")
        except Exception as e:
            logger.error(f"Failed to notify mothership: {e}")


@app.on_event("startup")
async def startup_event():
    """Initialize database, migrate users, and compile LangGraph workflows."""
    # Initialize SQLite database
    logger.info("Initializing SQLite database...")
    init_db()

    # Migrate auth.json if it exists
    with Session(engine) as session:
        migrate_auth_json(session, LEGACY_AUTH_FILE)

    # Compile all LangGraph workflows once at startup (singleton pattern)
    logger.info("Compiling LangGraph workflows...")
    try:
        checkpointer = get_or_create_async_checkpointer()

        # Import all build_workflow functions
        from app.agents.llamabot.nodes import build_workflow as build_llamabot
        from app.agents.llamapress.nodes import build_workflow as build_llamapress
        from app.agents.leonardo.rails_agent.nodes import build_workflow as build_rails_agent

        # Compile once and cache - these are thread-safe singletons
        app.state.compiled_graphs = {
            "llamabot": build_llamabot(checkpointer=checkpointer),
            "llamapress": build_llamapress(checkpointer=checkpointer),
            "rails_agent": build_rails_agent(checkpointer=checkpointer),
        }

        # Optional agents (may not exist in all deployments)
        try:
            from app.agents.leonardo.rails_ai_builder_agent.nodes import build_workflow as build_rails_ai_builder
            app.state.compiled_graphs["rails_ai_builder_agent"] = build_rails_ai_builder(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_ai_builder_agent not found, skipping")

        try:
            from app.agents.leonardo.rails_frontend_starter_agent.nodes import build_workflow as build_rails_frontend
            app.state.compiled_graphs["rails_frontend_starter_agent"] = build_rails_frontend(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_frontend_starter_agent not found, skipping")

        try:
            from app.agents.leonardo.rails_user_feedback_agent.nodes import build_workflow as build_rails_user_feedback
            app.state.compiled_graphs["rails_user_feedback_agent"] = build_rails_user_feedback(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_user_feedback_agent not found, skipping")

        try:
            from app.agents.leonardo.rails_ticket_mode_agent.nodes import build_workflow as build_rails_ticket_mode
            app.state.compiled_graphs["rails_ticket_mode_agent"] = build_rails_ticket_mode(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_ticket_mode_agent not found, skipping")

        try:
            from app.agents.leonardo.rails_testing_agent.nodes import build_workflow as build_rails_testing
            app.state.compiled_graphs["rails_testing_agent"] = build_rails_testing(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_testing_agent not found, skipping")

        logger.info(f"Compiled {len(app.state.compiled_graphs)} LangGraph workflows: {list(app.state.compiled_graphs.keys())}")
    except Exception as e:
        logger.error(f"Error compiling LangGraph workflows: {e}", exc_info=True)
        # Don't fail startup - fall back to per-request compilation
        app.state.compiled_graphs = {}

    # Log streaming disabled for now
    pass
    # asyncio.create_task(tail_docker_logs())
    # asyncio.create_task(broadcast_logs())
    # logger.info("Docker log streaming started")

    # Start lease manager background task
    await app.state.lease_manager.start()

    # Register signal handlers for graceful shutdown
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(graceful_shutdown(s))
            )
        logger.info("Signal handlers registered for graceful shutdown")
    except NotImplementedError:
        # Windows doesn't support add_signal_handler
        logger.warning("Signal handlers not supported on this platform")


# Mount MCP Server (stubbed out for now - MCP not yet fully implemented)
# from app.mcp_server import mcp
# app.mount("/mcp", mcp.sse_app(mount_path=""))
