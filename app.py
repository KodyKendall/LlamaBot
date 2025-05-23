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
from datetime import datetime
from agents.nodes import build_workflow

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

# Add CORS middleware to allow streaming from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize the ChatOpenAI client
llm = ChatOpenAI(
    model="o4-mini-2025-04-16"
)

client = Client(api_key=os.getenv("LANGSMITH_API_KEY"))

# Pydantic model for chat request
class ChatMessage(BaseModel):
    message: str

@app.get("/")
async def root():
    return {"message": "hello world"}

@app.post("/chat-message")
async def chat_message(chat_message: ChatMessage):
    request_id = f"req_{int(time.time())}_{hash(chat_message.message)%1000}"
    logger.info(f"[{request_id}] New chat message received: {chat_message.message[:50]}...")
    
    # Get the existing HTML content from page.html
    try:
        with open("page.html", "r") as f:
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

            # Build graph and create stream
            graph = build_workflow()
            stream = graph.stream({
                "messages": [HumanMessage(content=chat_message.message)],
                "initial_user_message": chat_message.message,
                "existing_html_content": existing_html_content
            }, stream_mode=["updates", "messages"]) # "values" is the third option ( to return the entire state object )

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

                            # Send node update
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

                            # Send node update
                            yield json.dumps({
                                "type": "update",
                                "node": node_step_name,
                                "value": str(updated_langgraph_state_object)  # Convert value to string for safety
                            }) + "\n"
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
    with open("page.html") as f:
        return f.read()