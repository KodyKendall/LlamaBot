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
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:3000", "http://127.0.0.1:3001"],  # React dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static directories
assets_dir = Path(__file__).parent / "assets"
app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

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
    with open("chat.html") as f:
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

@app.post("/llamabot-chat-message")
async def llamabot_chat_message(chat_message: dict, username: str = Depends(auth)): #NOTE: This could be arbitrary JSON, depending on the agent that we're using.
    thread_id = chat_message.get("thread_id") or "5"
    request_id = f"req_{int(time.time())}_{hash(chat_message.get('message'))%1000}"
    
    # Get the queue for this thread
    queue = thread_queues[thread_id]
    
    # If queue is full, return error
    if queue.qsize() >= MAX_QUEUE_SIZE:
        return JSONResponse({
            "error": "Too many pending messages",
            "request_id": request_id
        }, status_code=429)
    
    # Add message to queue
    await queue.put((request_id, chat_message))
    
    async def response_generator():
        try:
            # Get the lock for this thread
            async with thread_locks[thread_id]:
                logger.info(f"[{request_id}] Processing message from queue for thread {thread_id}")
                
                # Get our message from the queue
                current_request_id, current_message = await queue.get()
                if current_request_id != request_id:
                    # This shouldn't happen due to the lock, but just in case
                    logger.error(f"[{request_id}] Queue mismatch!")
                    return
                
                try:
                    checkpointer = get_or_create_async_checkpointer()
                    graph, state = get_langgraph_app_and_state_helper(current_message)
                    
                    yield json.dumps({
                        "type": "start",
                        "content": "start",
                        "request_id": request_id
                    }) + "\n"

                    stream = graph.astream(state,
                        config={"configurable": {"thread_id": thread_id}},
                        stream_mode=["updates"]
                    )

                    async for chunk in stream:
                        # Your existing chunk processing code...
                        is_this_chunk_an_llm_message = isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == 'messages'
                        is_this_chunk_an_update_stream_type = isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == 'updates'
                        
                        if is_this_chunk_an_update_stream_type:
                            state_object = chunk[1]
                            logger.info(f"🧠🧠🧠 LangGraph Output (State Update): {state_object}")

                            for agent_key, agent_data in state_object.items():
                                if isinstance(agent_data, dict) and 'messages' in agent_data:
                                    messages = agent_data['messages']
                                    base_message_as_dict = dumpd(messages[0])["kwargs"]
                                    yield json.dumps(base_message_as_dict) + "\n"

                finally:
                    # Mark task as done
                    queue.task_done()
                    
                    yield json.dumps({
                        "type": "final",
                        "content": "final"
                    }) + "\n"

        except Exception as e:
            logger.error(f"[{request_id}] Error in stream: {str(e)}", exc_info=True)
            yield json.dumps({
                "type": "error",
                "content": str(e)
            }) + "\n"

    return StreamingResponse(
        response_generator(),
        media_type="text/event-stream"
    )

@app.get("/chat", response_class=HTMLResponse)
async def chat(username: str = Depends(auth)):
    with open("chat.html") as f:
        return f.read()

@app.get("/page", response_class=HTMLResponse)
async def page(username: str = Depends(auth)):
    with open("page.html") as f:
        return f.read()
    
@app.get("/agent_page/{agent_name}", response_class=HTMLResponse)
async def agent_page(agent_name: str, username: str = Depends(auth)):
    from pathlib import Path
    # Get the absolute path to the project root
    project_root = Path(__file__).parent.parent
    page_path = project_root / "app" / "agents" / agent_name / "page.html"
    if not page_path.exists():
        raise HTTPException(status_code=404, detail=f"Page not found for agent: {agent_name}")
    with open(page_path) as f:
        return f.read()

@app.get("/logs", response_class=HTMLResponse)
async def logs_viewer(username: str = Depends(auth)):
    with open("logs_viewer.html") as f:
        return f.read()

# Docker-native log streaming infrastructure
clients = set()  # Set of client queues
log_queue = asyncio.Queue(maxsize=2000)  # Central buffer

async def tail_docker_logs():
    """Tail Rails container logs using docker logs --follow (single shared subprocess)"""
    container_name = "leonardo-llamapress-1"

    while True:  # Auto-restart on failure
        try:
            logger.info(f"Starting Docker log tailer for container: {container_name}")
            process = await asyncio.create_subprocess_exec(
                'docker', 'logs', '--follow', '--tail', '0', container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            async def read_stream(stream, stream_name):
                """Read from stdout or stderr"""
                async for line in stream:
                    try:
                        entry = line.decode('utf-8', errors='ignore').strip()
                        if not entry:
                            continue

                        log_entry = {
                            'type': 'log',
                            'source': 'rails',
                            'message': entry,
                            'timestamp': datetime.now(timezone.utc).isoformat(),
                            'level': 'INFO'
                        }

                        # Add to central queue with FIFO eviction
                        try:
                            log_queue.put_nowait(log_entry)
                        except asyncio.QueueFull:
                            # Evict oldest, add newest
                            try:
                                log_queue.get_nowait()
                                log_queue.put_nowait(log_entry)
                            except:
                                pass
                    except Exception as e:
                        logger.error(f"Error processing log line: {e}")

            # Read both stdout and stderr concurrently
            await asyncio.gather(
                read_stream(process.stdout, 'stdout'),
                read_stream(process.stderr, 'stderr'),
                return_exceptions=True
            )

            await process.wait()
            logger.warning(f"Docker log tailer exited with code {process.returncode}")

        except Exception as e:
            logger.error(f"Error in Docker log tailer: {e}")

        # Wait before restarting
        logger.info("Restarting Docker log tailer in 5 seconds...")
        await asyncio.sleep(5)

async def broadcast_logs():
    """Fan-out logs from central queue to all connected SSE clients"""
    while True:
        try:
            log_entry = await log_queue.get()

            # Broadcast to all connected clients
            for client_queue in list(clients):
                try:
                    client_queue.put_nowait(log_entry)
                except asyncio.QueueFull:
                    pass  # Skip slow clients
        except Exception as e:
            logger.error(f"Error in broadcast_logs: {e}")
            await asyncio.sleep(0.1)

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
        from app.agents.rails_agent.nodes import build_workflow as build_rails_agent

        # Compile once and cache - these are thread-safe singletons
        app.state.compiled_graphs = {
            "llamabot": build_llamabot(checkpointer=checkpointer),
            "llamapress": build_llamapress(checkpointer=checkpointer),
            "rails_agent": build_rails_agent(checkpointer=checkpointer),
        }

        # Optional agents (may not exist in all deployments)
        try:
            from app.agents.rails_ai_builder_agent.nodes import build_workflow as build_rails_ai_builder
            app.state.compiled_graphs["rails_ai_builder_agent"] = build_rails_ai_builder(checkpointer=checkpointer)
        except ImportError:
            logger.info("rails_ai_builder_agent not found, skipping")

        try:
            from app.agents.rails_frontend_starter_agent.nodes import build_workflow as build_rails_frontend
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
        state_history.append({"thread_id": thread_id, "state": graph.get_state(config=config)})

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
    state_history = graph.get_state(config=config) #graph.get_state returns a StateSnapshot object, which inherits from a Named Tuple. Serializes into an Array.
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
    """Parse routes.rb and return available GET routes for index actions and home page"""
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

@app.get("/logs/stream")
async def stream_logs(username: str = Depends(auth)):
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