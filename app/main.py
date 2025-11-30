from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import os
import logging
import time
import json
import asyncio
import bcrypt
import secrets
from datetime import datetime, timezone
from pathlib import Path

from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import ConnectionPool
from pydantic import BaseModel
from dotenv import load_dotenv

from langchain_core.load import dumpd
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver

from app.agents.llamabot.nodes import build_workflow
from app.websocket.web_socket_connection_manager import WebSocketConnectionManager
from app.websocket.web_socket_handler import WebSocketHandler
from app.websocket.request_handler import RequestHandler
from collections import defaultdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('chat_app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

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

# Initialize HTTP Basic Auth
security = HTTPBasic()

# Path to auth file
AUTH_FILE = Path(__file__).parent / "auth.json"

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
                logger.info("✅ Connected to PostgreSQL for persistence")
            except Exception as e:
                logger.warning(f"❌ DB_URI: {db_uri}")
                logger.warning(f"❌ PostgreSQL unavailable ({str(e).split(':', 1)[0]}). Using MemorySaver for session-based persistence.")
                app.state.checkpointer = MemorySaver()
        else:
            logger.info("📝 No DB_URI configured. Using MemorySaver for session-based persistence.")
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
                logger.info("✅ Connected to PostgreSQL (async) for persistence")
            except Exception as e:
                logger.warning(f"❌ DB_URI: {db_uri}")
                logger.warning(f"❌ PostgreSQL unavailable for async operations ({str(e).split(':', 1)[0]}). Using MemorySaver for session-based persistence.")
                app.state.async_checkpointer = MemorySaver()
        else:
            logger.info("📝 No DB_URI configured. Using MemorySaver for async session-based persistence.")
            app.state.async_checkpointer = MemorySaver()
    
    return app.state.async_checkpointer

def get_langgraph_app_and_state_helper(message: dict):
    """Helper function to access RequestHandler.get_langgraph_app_and_state from main.py"""
    request_handler = RequestHandler(app)
    response = request_handler.get_langgraph_app_and_state(message)
    return response

def auth(credentials: HTTPBasicCredentials = Depends(security)):
    """Validate HTTP Basic Auth credentials against stored auth.json"""
    # Check if auth file exists
    if not AUTH_FILE.exists():
        logger.warning("No auth.json file found. Please register first.")
        raise HTTPException(
            status_code=401,
            detail="Authentication not configured",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    try:
        # Load stored credentials
        with open(AUTH_FILE, 'r') as f:
            stored_auth = json.load(f)
    except Exception as e:
        logger.error(f"Error reading auth.json: {e}")
        raise HTTPException(
            status_code=500,
            detail="Authentication error"
        )
    
    # Verify username using constant-time comparison
    username_correct = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        stored_auth.get("username", "").encode("utf-8")
    )
    
    # Verify password
    password_correct = False
    if username_correct and "password_hash" in stored_auth:
        password_correct = bcrypt.checkpw(
            credentials.password.encode("utf-8"),
            stored_auth["password_hash"].encode("utf-8")
        )
    
    if not (username_correct and password_correct):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    return credentials.username

# At module level
thread_locks = defaultdict(asyncio.Lock)
thread_queues = defaultdict(asyncio.Queue)
MAX_QUEUE_SIZE = 10

@app.get("/", response_class=HTMLResponse)
async def root(username: str = Depends(auth)):
    # Serve the chat.html file
    with open(frontend_dir / "chat.html") as f:
        return f.read()
    
@app.get("/hello", response_class=JSONResponse)
async def hello():
    return {"message": "Hello, World! 🦙💬"}

@app.get("/register", response_class=HTMLResponse)
async def register_form():
    """Serve the registration form"""
    with open("register.html") as f:
        html = f.read()
        return HTMLResponse(content=html)

@app.post("/register")
async def register(
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...)
):
    """Process registration form"""
    # Check if auth.json already exists
    if AUTH_FILE.exists():
        raise HTTPException(
            status_code=400,
            detail="Authentication already configured. Delete auth.json to re-register."
        )
    
    # Validate passwords match
    if password != confirm:
        raise HTTPException(
            status_code=400,
            detail="Passwords do not match"
        )
    
    # Hash the password
    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    
    # Create auth data
    auth_data = {
        "username": username,
        "password_hash": password_hash.decode('utf-8')
    }
    
    try:
        # Write to auth.json
        with open(AUTH_FILE, 'w') as f:
            json.dump(auth_data, f, indent=2)
        
        logger.info(f"User '{username}' registered successfully")
        return JSONResponse({
            "message": f"User '{username}' registered successfully! You can now use these credentials to access protected endpoints."
        })
    except Exception as e:
        logger.error(f"Error writing auth.json: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to save authentication data"
        )

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await WebSocketHandler(websocket, manager).handle_websocket()

@app.on_event("startup")
async def startup_log_streaming():
    """Start background tasks for log streaming and compile LangGraph workflows"""
    # Compile all LangGraph workflows once at startup (singleton pattern)
    logger.info("🔨 Compiling LangGraph workflows...")
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

        logger.info(f"✅ Compiled {len(app.state.compiled_graphs)} LangGraph workflows: {list(app.state.compiled_graphs.keys())}")
    except Exception as e:
        logger.error(f"❌ Error compiling LangGraph workflows: {e}", exc_info=True)
        # Don't fail startup - fall back to per-request compilation
        app.state.compiled_graphs = {}

    # Log streaming disabled for now
    pass
    # asyncio.create_task(tail_docker_logs())
    # asyncio.create_task(broadcast_logs())
    # logger.info("✅ Docker log streaming started")

@app.get("/conversations", response_class=HTMLResponse)
async def conversations(username: str = Depends(auth)):
    with open("conversations.html") as f:
        return f.read()

@app.get("/threads", response_class=JSONResponse)
async def threads(username: str = Depends(auth)):
    checkpointer = get_or_create_checkpointer()
    config = {}
    checkpoint_generator = checkpointer.list(config=config)

    # ❌ OLD: all_checkpoints = list(checkpoint_generator)  # Loads ALL checkpoints into memory!
    # ✅ NEW: Stream through generator to extract unique thread_ids without loading all data
    unique_thread_ids = set()
    for checkpoint in checkpoint_generator:
        thread_id = checkpoint[0]["configurable"]["thread_id"]
        unique_thread_ids.add(thread_id)

    unique_thread_ids = list(unique_thread_ids)
    logger.info(f"📊 Found {len(unique_thread_ids)} unique threads (memory-efficient extraction)")

    # Optional: Limit threads returned to prevent excessive memory usage
    MAX_THREADS = 100
    if len(unique_thread_ids) > MAX_THREADS:
        logger.warning(f"⚠️ Limiting threads response from {len(unique_thread_ids)} to {MAX_THREADS} threads")
        unique_thread_ids = unique_thread_ids[:MAX_THREADS]

    state_history = []

    # ✅ Use cached graph from startup (singleton pattern)
    graph = app.state.compiled_graphs.get("llamabot")
    if not graph:
        from app.agents.llamabot.nodes import build_workflow
        graph = build_workflow(checkpointer=checkpointer)
        logger.warning("⚠️ /threads endpoint using fallback graph compilation")

    # Get only the LATEST state for each thread (not full history)
    for thread_id in unique_thread_ids:
        config = {"configurable": {"thread_id": thread_id}}
        state_history.append({"thread_id": thread_id, "state": await graph.aget_state(config=config)})

    return state_history

@app.get("/chat-history/{thread_id}")
async def chat_history(thread_id: str, username: str = Depends(auth)):
    checkpointer = get_or_create_checkpointer()

    # ✅ Use cached graph from startup (singleton pattern)
    graph = app.state.compiled_graphs.get("llamabot")
    if not graph:
        # Fallback: compile once if not in cache
        from app.agents.llamabot.nodes import build_workflow
        graph = build_workflow(checkpointer=checkpointer)
        logger.warning("⚠️ /chat-history endpoint using fallback graph compilation")

    config = {"configurable": {"thread_id": thread_id}}
    state_history = await graph.aget_state(config=config) #graph.aget_state returns a StateSnapshot object, which inherits from a Named Tuple. Serializes into an Array.
    print(state_history)
    return state_history

@app.get("/available-agents", response_class=JSONResponse)
async def available_agents():
    # map from langgraph.json to a list of agent names
    with open("langgraph.json", "r") as f:
        langgraph_json = json.load(f)
    return {"agents": list(langgraph_json["graphs"].keys())}

@app.get("/rails-routes", response_class=JSONResponse)
async def rails_routes():
    """Parse routes.rb and return available GET routes for `index` actions and home page"""
    import re
    import os

    routes = []
    routes_file = "rails/config/routes.rb"

    # Check if routes file exists
    if not os.path.exists(routes_file):
        return {"routes": [{"path": "/", "name": "Home"}]}

    try:
        with open(routes_file, "r") as f:
            content = f.read()

        # Extract root path
        root_match = re.search(r'root\s+"([^"]+)#([^"]+)"', content)
        if root_match:
            routes.append({"path": "/", "name": "Home"})

        # Extract explicit home route
        home_match = re.search(r'get\s+"home"\s*=>', content)
        if home_match and not any(r["path"] == "/" for r in routes):
            routes.append({"path": "/", "name": "Home"})

        # Extract resources (which create index routes)
        resource_matches = re.findall(r'resources\s+:(\w+)', content)
        for resource in resource_matches:
            routes.append({
                "path": f"/{resource}",
                "name": resource.capitalize()
            })

        # Extract custom GET routes
        get_matches = re.findall(r'get\s+"([^"]+)"\s*=>\s*"([^"]+)#([^"]+)"', content)
        for path, controller, action in get_matches:
            if path not in ["/", "home", "up", "service-worker", "manifest"]:
                display_name = path.replace("/", "").replace("_", " ").title() or "Home"
                routes.append({
                    "path": f"/{path}" if not path.startswith("/") else path,
                    "name": display_name
                })

        # Remove duplicates based on path
        seen = set()
        unique_routes = []
        for route in routes:
            if route["path"] not in seen:
                seen.add(route["path"])
                unique_routes.append(route)

        return {"routes": unique_routes}

    except Exception as e:
        print(f"Error parsing routes: {e}")
        return {"routes": [{"path": "/", "name": "Home"}]}

@app.get("/check")
def check_timestamp():
    # returns the timestamp of last message from user in utc
    return {"timestamp": app.state.timestamp}
    """Stream real-time Rails logs via Docker to browser using SSE"""

    async def log_generator():
        # Create queue for this client
        client_queue = asyncio.Queue(maxsize=500)
        clients.add(client_queue)

        try:
            # Send initial connection message
            yield f"data: {json.dumps({'type': 'connected', 'sources': ['rails']})}\n\n"

            # Stream logs as they arrive from broadcast
            while True:
                try:
                    log_entry = await asyncio.wait_for(client_queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(log_entry)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive to prevent connection timeout
                    yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"

        except asyncio.CancelledError:
            logger.info("Log streaming cancelled by client")
        except Exception as e:
            logger.error(f"Error in log stream: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            # Cleanup: remove client queue
            clients.discard(client_queue)
            logger.info(f"Client disconnected. Active clients: {len(clients)}")

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

# Mount MCP Server
from app.mcp_server import mcp

# Mount the MCP server's SSE application
app.mount("/mcp", mcp.sse_app(mount_path=""))