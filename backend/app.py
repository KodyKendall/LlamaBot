from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage
from pydantic import BaseModel
from dotenv import load_dotenv
import os
import logging
import time
import json
import importlib
from datetime import datetime
from pathlib import Path
from agents.react_agent.nodes import build_workflow
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.base import CheckpointTuple
from psycopg_pool import ConnectionPool

from langsmith import Client

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
app.mount("/assets", StaticFiles(directory="../assets"), name="assets")

# Global variable to track loaded artifacts
loaded_artifacts = {}

# Dynamic artifact loading function
def load_artifacts():
    """Dynamically discover and load artifact modules"""
    artifacts_dir = Path("artifacts")
    
    if not artifacts_dir.exists():
        logger.info("No artifacts directory found, skipping artifact loading")
        return
    
    for item in artifacts_dir.iterdir():
        if item.is_dir() and not item.name.startswith('.'):
            module_name = f"artifacts.{item.name}"
            init_file = item / "__init__.py"
            
            if init_file.exists():
                try:
                    logger.info(f"Loading artifact module: {module_name}")
                    module = importlib.import_module(module_name)
                    
                    artifact_info = {
                        "module_name": module_name,
                        "static_mounted": False,
                        "router_included": False,
                        "metadata": None
                    }
                    
                    # Get artifact metadata if available
                    if hasattr(module, 'get_artifact_info'):
                        artifact_info["metadata"] = module.get_artifact_info()
                    
                    # Mount static files if the function exists
                    if hasattr(module, 'mount_static_files'):
                        logger.info(f"Mounting static files for {module_name}")
                        success = module.mount_static_files(app)
                        artifact_info["static_mounted"] = success
                    
                    # Include router if it exists
                    if hasattr(module, 'router'):
                        prefix = f"/api/{item.name}"
                        logger.info(f"Including router for {module_name} with prefix {prefix}")
                        app.include_router(module.router, prefix=prefix)
                        artifact_info["router_included"] = True
                        artifact_info["api_prefix"] = prefix
                    
                    loaded_artifacts[item.name] = artifact_info
                    logger.info(f"Successfully loaded artifact: {module_name}")
                    
                except Exception as e:
                    logger.error(f"Failed to load artifact {module_name}: {e}")
                    loaded_artifacts[item.name] = {
                        "module_name": module_name,
                        "error": str(e),
                        "status": "failed"
                    }

# Load all artifacts dynamically
load_artifacts()

# Initialize the ChatOpenAI client
llm = ChatOpenAI(
    model="o4-mini-2025-04-16"
)

client = Client(api_key=os.getenv("LANGSMITH_API_KEY"))

# Pydantic model for chat request
class ChatMessage(BaseModel):
    message: str
    thread_id: str = None  # Optional thread_id parameter
    agent: str = None  # Optional agent parameter

# Application state to hold persistent checkpointer, important for session-based persistence.
app.state.checkpointer = None

def get_or_create_checkpointer():
    """Get persistent checkpointer, creating once if needed"""
    if app.state.checkpointer is None:
        db_uri = os.getenv("DB_URI")
        if db_uri:
            try:
                # Create connection pool and PostgresSaver directly
                pool = ConnectionPool(db_uri)
                app.state.checkpointer = PostgresSaver(pool)
                app.state.checkpointer.setup()
                logger.info("Using PostgreSQL persistence.")
            except Exception as e:
                logger.warning(f"Failed to connect to PostgreSQL: {e}. Using MemorySaver.")
                app.state.checkpointer = MemorySaver()
        else:
            logger.info("No DB_URI found. Using MemorySaver for session-based persistence.")
            app.state.checkpointer = MemorySaver()
    
    return app.state.checkpointer

@app.get("/", response_class=HTMLResponse)
async def root():
    # Serve the home.html file
    with open("home.html") as f:
        return f.read()

@app.post("/chat-message")
async def chat_message(chat_message: ChatMessage):
    request_id = f"req_{int(time.time())}_{hash(chat_message.message)%1000}"
    logger.info(f"[{request_id}] New chat message received: {chat_message.message[:50]}...")
    
    # Get the existing HTML content from page.html
    try:
        with open("../page.html", "r") as f:
            existing_html_content = f.read()
    except FileNotFoundError:
        existing_html_content = ""

    # Define a generator function to stream the response
    async def response_generator():
        try:
            logger.info(f"[{request_id}] Starting streaming response")

            # Initial response with request ID
            yield json.dumps({
                "type": "start",
                "request_id": request_id
            }) + "\n"

            # Use the provided thread_id or default to "5"
            thread_id = chat_message.thread_id or "5"
            logger.info(f"[{request_id}] Using thread_id: {thread_id}")
            
            checkpointer = get_or_create_checkpointer()
            graph = build_workflow(checkpointer=checkpointer)
            stream = graph.stream({
                "messages": [HumanMessage(content=chat_message.message)],
                "initial_user_message": chat_message.message,
                "existing_html_content": existing_html_content
                }, 
                config={"configurable": {"thread_id": thread_id}},
                stream_mode=["updates", "messages"] # "values" is the third option ( to return the entire state object )
            ) 

            # Track the final state to serialize at the end
            final_state = None

            # Stream each chunk
            for chunk in stream:
                if chunk is not None:

                    is_this_chunk_an_llm_message = isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == 'messages'
                    is_this_chunk_an_update_stream_type = isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == 'updates'

                    if is_this_chunk_an_llm_message: ## If this is a message from the LLM. (Known as a 'Messages' Chunk streaming from LangGraph

                        # Get the langgraph_node_info.
                        langgraph_node_info = chunk[1][1] # this is dict object with keys => dict_keys(['langgraph_step', 'langgraph_node', 'langgraph_triggers', 'langgraph_path', 'langgraph_checkpoint_ns', 'checkpoint_ns', 'ls_provider', 'ls_model_name', 'ls_model_type', 'ls_temperature'])
                        
                        # Get the AI message from the LLM.
                        message_from_llm = chunk[1][0] #AIMessageChunk object -> https://python.langchain.com/api_reference/core/messages/langchain_core.messages.ai.AIMessageChunk.html

                        # Handle tuple format (node, value)
                        node, value = chunk
                        if value is not None:
                            # Log the streaming output
                            logger.info(f"[{request_id}] Stream update from {langgraph_node_info['langgraph_node']}: {str(message_from_llm)[:100]}...")

                            # Send streaming update for React frontend
                            yield json.dumps({
                                "type": "update",
                                "node": langgraph_node_info['langgraph_node'],
                                "value": str(message_from_llm.content)  # Convert value to string for safety
                            }) + "\n"
                    
                    elif is_this_chunk_an_update_stream_type:
                        updated_langgraph_state_object = chunk[1] # Dict object
                        
                        node_step_name = list(chunk[1].keys())[-1] # will be one of the following:'route_initial_user_message', 'respond_naturally', 'design_and_plan', 'write_html_code'
                        
                        node, value = chunk
                        if updated_langgraph_state_object is not None:
                            # Log the streaming output
                            logger.info(f"[{request_id}] Stream update from {node}: {str(value)[:100]}...")

                            # Store final state for the final response
                            if 'messages' in updated_langgraph_state_object:
                                final_state = updated_langgraph_state_object

                    else:
                        # Handle other formats or just log
                        logger.info(f"[{request_id}] Received chunk in unknown format: {type(chunk)}")
        except Exception as e:
            logger.error(f"[{request_id}] Error in stream: {str(e)}", exc_info=True)
            yield json.dumps({
                "type": "error",
                "error": str(e),
                "request_id": request_id
            }) + "\n"
        finally:
            logger.info(f"[{request_id}] Stream completed")
            # Send final update with complete messages
            yield json.dumps({
                "type": "final",
                "node": "final",
                "value": "final",
                "messages": final_state.get("messages", []) if final_state else []
            }) + "\n"

    # Return a streaming response
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
    with open("../page.html") as f:
        return f.read()

@app.get("/artifacts/{project_name}/page", response_class=HTMLResponse)
async def artifact_page(project_name: str):
    """Serve the page.html file for a specific artifact project"""
    artifact_path = Path(f"artifacts/{project_name}/page.html")
    if artifact_path.exists():
        with open(artifact_path) as f:
            return f.read()
    else:
        return HTMLResponse("Artifact page not found", status_code=404)
    
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
        state = graph.get_state(config=config)  # this is a StateSnapshot object: https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py#L211
        # StateSnapshot inherits from NamedTuple. NamedTuples serialize as an array by default, so we use _asdict to return keys for easier understanding on the client side.
        # Convert StateSnapshot NamedTuple to dict for proper JSON serialization
        state_dict = state._asdict() if hasattr(state, '_asdict') else state
        state_history.append({"thread_id": thread_id, "state": state_dict})

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

@app.get("/artifacts", response_class=JSONResponse)
async def list_artifacts():
    """List all loaded artifact modules and their status"""
    return {
        "artifacts": loaded_artifacts,
        "count": len(loaded_artifacts),
        "loaded_successfully": len([a for a in loaded_artifacts.values() if "error" not in a])
    }