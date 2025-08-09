from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import asyncio

from langchain_core.load import dumpd
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage
from langsmith import Client

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver

from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from psycopg_pool import ConnectionPool

from pydantic import BaseModel
from dotenv import load_dotenv

import os
import logging
import time
import json

from datetime import datetime
from app.agents.react_agent.nodes import build_workflow
from app.agents.llamabot_v1.nodes import build_workflow as build_workflow_llamabot_v1
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
    allow_origins=["http://localhost:3001", "http://127.0.0.1:3001"],  # React dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static directories
# app.mount("/assets", StaticFiles(directory="../assets"), name="assets")
# app.mount("/examples", StaticFiles(directory="../examples"), name="examples")

# Initialize the ChatOpenAI client
llm = ChatOpenAI(
    model="o4-mini-2025-04-16"
)

client = Client(api_key=os.getenv("LANGSMITH_API_KEY"))

# This is responsible for holding and managing all active websocket connections.
manager = WebSocketConnectionManager(app) 

# Pydantic model for chat request
class ChatMessage(BaseModel):
    message: str
    thread_id: str = None  # Optional thread_id parameter
    agent: str = None  # Optional agent parameter

# Application state to hold persistent checkpointer, important for session-based persistence.
app.state.checkpointer = None
app.state.async_checkpointer = None

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
                pool = ConnectionPool(
                    db_uri,
                    min_size=1,
                    max_size=5,
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
                pool = AsyncConnectionPool(
                    db_uri,
                    min_size=1,
                    max_size=5,
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

# At module level
thread_locks = defaultdict(asyncio.Lock)
thread_queues = defaultdict(asyncio.Queue)
MAX_QUEUE_SIZE = 10

@app.get("/", response_class=HTMLResponse)
async def root():
    # Serve the chat.html file
    with open("chat.html") as f:
        return f.read()
    
@app.get("/hello", response_class=JSONResponse)
async def hello():
    return {"message": "Hello, World! 🦙💬"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await WebSocketHandler(websocket, manager).handle_websocket()

@app.post("/llamabot-chat-message")
async def llamabot_chat_message(chat_message: dict): #NOTE: This could be arbitrary JSON, depending on the agent that we're using.
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
async def chat():
    with open("chat.html") as f:
        return f.read()

@app.get("/page", response_class=HTMLResponse)
async def page():
    with open("page.html") as f:
        return f.read()
    
@app.get("/conversations", response_class=HTMLResponse)
async def conversations():
    with open("conversations.html") as f:
        return f.read()

@app.get("/threads", response_class=JSONResponse)
async def threads():
    checkpointer = get_or_create_checkpointer()
    config = {}
    checkpoint_generator = checkpointer.list(config=config)
    all_checkpoints :list[CheckpointTuple] = list(checkpoint_generator) #convert to list
    
    # reduce only to the unique thread_ids  
    unique_thread_ids = list(set([checkpoint[0]["configurable"]["thread_id"] for checkpoint in all_checkpoints]))
    state_history = []
    for thread_id in unique_thread_ids:
        graph = build_workflow(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        state_history.append({"thread_id": thread_id, "state": graph.get_state(config=config)})
    return state_history

@app.get("/chat-history/{thread_id}")
async def chat_history(thread_id: str):
    checkpointer = get_or_create_checkpointer()
    graph = build_workflow(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    state_history = graph.get_state(config=config)
    print(state_history)
    return state_history

@app.get("/available-agents", response_class=JSONResponse)
async def available_agents():
    # map from langgraph.json to a list of agent names
    with open("../langgraph.json", "r") as f:
        langgraph_json = json.load(f)
    return {"agents": list(langgraph_json["graphs"].keys())}