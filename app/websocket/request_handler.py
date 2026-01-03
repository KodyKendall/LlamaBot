from asyncio import Lock, CancelledError

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketState

from app.websocket.web_socket_request_context import WebSocketRequestContext
from typing import Dict, Optional

from langchain_core.messages import HumanMessage
from langgraph.graph import MessagesState
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langchain_core.load import dumpd
from psycopg_pool import AsyncConnectionPool
from datetime import datetime, timezone

from pathlib import Path

from dotenv import load_dotenv
import json
import importlib
import os
import logging

logger = logging.getLogger(__name__)

load_dotenv()

from typing import Any, Dict, TypedDict

class RequestHandler:
    def __init__(self, app: FastAPI):
        self.locks: Dict[int, Lock] = {}
        self.app = app
    
    def _get_lock(self, websocket: WebSocket) -> Lock:
        """Get or create a lock for a specific websocket connection"""
        ws_id = id(websocket)
        if ws_id not in self.locks:
            self.locks[ws_id] = Lock()
        return self.locks[ws_id]

    def _is_websocket_open(self, websocket: WebSocket) -> bool:
        """Check if the WebSocket connection is still open"""
        return websocket.client_state == WebSocketState.CONNECTED

    # This is a the main function that handles incoming WebSocket requests. This will build the LangGraph workflow, and invoke it from a checkpointed state.
    async def handle_request(self, message: dict, websocket: WebSocket):
        """Handle incoming WebSocket requests with proper locking and cancellation"""
        ws_id = id(websocket)
        lock = self._get_lock(websocket)

        self.app.state.timestamp = datetime.now(timezone.utc) # keep timestamp updated
        
        async with lock:
            try:
                app, state, agent_config = self.get_langgraph_app_and_state(message)

                # Default limits (can be overridden per-agent in langgraph.json)
                DEFAULT_RECURSION_LIMIT = 200

                # Get agent-specific limits or use defaults
                recursion_limit = agent_config.get("recursion_limit", DEFAULT_RECURSION_LIMIT)

                # Note: Message history management is now handled by SummarizationMiddleware
                # in each agent's middleware stack, which provides intelligent summarization
                # instead of naive message trimming.

                config = {
                    "configurable": {
                        "thread_id": f"{message.get('thread_id')}",
                        "origin": message.get('origin', ''),
                        "recursion_limit": recursion_limit
                    },
                    "recursion_limit": recursion_limit
                }

                async for chunk in app.astream(state, config=config, stream_mode=["updates", "messages"], subgraphs=True):

                    # NOTE: In LangGraph 0.5, they introduced this "subgraphs" parameter, that changes the datashape if you set it to True.
                    # if subgraph=True, it returns a tuple with 3 elements, instead of 2 elements.
                    # the first element is the subgraph name, the second element is the streaming data type ["updates", "messages", "values"], and the third element is the actual metadata.

                    is_this_chunk_an_llm_message = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'messages'
                    is_this_chunk_an_update_stream_type = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'updates'
                    logger.info(f"🍅🍅🍅 Chunk: {chunk}")
                    
                    if is_this_chunk_an_llm_message:
                        message_chunk_from_llm = chunk[2][0] #AIMessageChunk object -> https://python.langchain.com/api_reference/core/messages/langchain_core.messages.ai.AIMessageChunk.html
                        data_type = "AIMessageChunk"
                        base_message_as_dict = dumpd(chunk[2][0])["kwargs"]

                        # Content format varies by LLM provider:
                        # - OpenAI: content is a string ("Hello world")
                        # - Anthropic/Claude: content is a list of content blocks [{type: "text", text: "..."}]
                        # - Gemini: content is a list of content blocks [{type: "text", text: "..."}, {type: "image_url", ...}]
                        # - Thinking/Reasoning blocks: [{type: "thinking"/"reasoning", text/thinking: "..."}]
                        content = base_message_as_dict["content"]
                        logger.info(f"🍅 Content type: {type(content)}, Content: {content}")

                        # Separate thinking/reasoning content from regular text content
                        # This allows the frontend to display thinking in a dedicated area
                        thinking_content = None
                        text_content = content

                        if isinstance(content, list):
                            # Extract thinking/reasoning blocks (varies by provider)
                            # - Claude: {type: "thinking", thinking: "..."}
                            # - OpenAI: {type: "reasoning", text: "..."}
                            # - Gemini (langchain-google-genai): {type: "thinking", thinking: "...", signature: "..."}
                            thinking_blocks = [
                                b for b in content
                                if isinstance(b, dict) and (
                                    b.get("type") == "reasoning" or
                                    b.get("type") == "thinking" or
                                    b.get("thought") == True
                                )
                            ]

                            # Extract regular text blocks
                            text_blocks = [
                                b for b in content
                                if isinstance(b, dict) and (
                                    b.get("type") in ("text", "text_delta") and
                                    not b.get("thought")
                                )
                            ]

                            if thinking_blocks:
                                thinking_content = thinking_blocks
                            # Always use text_blocks if we found any, otherwise use empty list
                            # This prevents thinking content from appearing in text_content
                            text_content = text_blocks if text_blocks else []

                        # Token usage is typically only available on the final chunk or update message
                        # For streaming chunks, we skip token extraction (Anthropic doesn't send it mid-stream)
                        token_usage = None

                        # # Only send if WebSocket is still open
                        if self._is_websocket_open(websocket):
                            ws_message = {
                                "type": base_message_as_dict["type"],
                                "content": text_content,  # Text content only (thinking separated)
                                "thinking": thinking_content,  # Thinking/reasoning content (may be None)
                                "tool_calls": [],
                                "base_message": base_message_as_dict
                            }
                            if token_usage:
                                ws_message["token_usage"] = token_usage
                            await websocket.send_json(ws_message)

                    elif is_this_chunk_an_update_stream_type: # This means that LangGraph has given us a state update. This will often include a new message from the AI.
                        state_object = chunk[2]
                        logger.info(f"🧠🧠🧠 LangGraph Output (State Update): {state_object}")

                        # Handle dynamic agent key - look for messages in any nested dict
                        messages = None
                        for agent_key, agent_data in state_object.items():
                            did_agent_have_a_message_for_us = isinstance(agent_data, dict) and 'messages' in agent_data

                            if did_agent_have_a_message_for_us:

                                #agent_data is a just the state dictionary. 
                                # We can access state objects by using agent_data['key']. 

                                messages = agent_data['messages'] #Question: is this ALL messages coming through, or just the latest AI message?

                                # Safe check for tool calls with better error handling
                                did_agent_evoke_a_tool = False
                                tool_calls = []
                                
                                if messages and len(messages) > 0:
                                    message = messages[-1] # get the latest message (the last one in the list. Sometimes we have a human message and an AI message, so we want the AI message, depending on if we're using the create_react_agent tool or not)
                                    if hasattr(message, 'additional_kwargs') and message.additional_kwargs:
                                        tool_calls_data = message.additional_kwargs.get('tool_calls')
                                        if tool_calls_data:
                                            did_agent_evoke_a_tool = True
                                            tool_calls = tool_calls_data
                                            
                                            # Log tool call details
                                            if len(tool_calls) > 0:
                                                tool_call_object = tool_calls[0]
                                                tool_call_name = tool_call_object.get("name")
                                                tool_call_args = tool_call_object.get("args")
                                                logger.info(f"🔨🔨🔨 Tool Call Name: {tool_call_name}")
                                                logger.info(f"🔨🔨🔨 Tool Call Args: {tool_call_args}")

                                    # AIMessage is not serializable to JSON, so we need to convert it to a string.
                                    messages_as_string = [msg.content if hasattr(msg, 'content') else str(msg) for msg in messages]

                                    #NOTE: I found we're able to serialize AIMessage into dict using dumpd.
                                    try:
                                        base_message_as_dict = dumpd(message)["kwargs"]
                                    except Exception as e:
                                        logger.warning(f"Failed to serialize message: {e}")
                                        base_message_as_dict = {"content": str(message), "type": "ai"}

                                    # Extract token usage metadata if available (for context window tracking)
                                    # Anthropic sends usage_metadata on the final AIMessage (not during streaming)
                                    token_usage = None
                                    usage_metadata = getattr(message, 'usage_metadata', None)

                                    if usage_metadata:
                                        token_usage = {
                                            "input_tokens": usage_metadata.get("input_tokens", 0) if isinstance(usage_metadata, dict) else getattr(usage_metadata, 'input_tokens', 0),
                                            "output_tokens": usage_metadata.get("output_tokens", 0) if isinstance(usage_metadata, dict) else getattr(usage_metadata, 'output_tokens', 0),
                                            "total_tokens": usage_metadata.get("total_tokens", 0) if isinstance(usage_metadata, dict) else getattr(usage_metadata, 'total_tokens', 0)
                                        }
                                        logger.info(f"📊 Token usage: {token_usage}")

                                    # Only send if WebSocket is still open
                                    if self._is_websocket_open(websocket):

                                        # NOTE: This JSON object is a standardized format that we've been using for all our front-ends.
                                        # Eventually, we might want to just rely on the base_message data shape as the source of truth for all front-ends.
                                        llamapress_user_interface_json = { # we're forcing this shape to match a BaseMessage type.
                                            "type": message.type if hasattr(message, 'type') else "ai",
                                            "content": messages_as_string[-1] if messages_as_string else "",
                                            "tool_calls": tool_calls,
                                            "base_message": base_message_as_dict
                                        }
                                        if token_usage:
                                            llamapress_user_interface_json["token_usage"] = token_usage

                                        await websocket.send_json(llamapress_user_interface_json)
                        logger.info(f"LangGraph Output (State Update): {chunk}")

                        # chunk will look like this:
                        # {'llamabot': {'messages': [AIMessage(content='Hello! I hear you loud and clear. I'm LlamaBot, your full-stack Rails developer assistant. How can I help you today?', additional_kwargs={}, response_metadata={'finish_reason': 'stop', 'model_name': 'o4-mini-2025-04-16', 'service_tier': 'default'}, id='run--ce385bc4-fecb-4127-81d2-1da5814874f8')]}}

                    else:
                        logger.info(f"Workflow output: {chunk}")

                print("🎏🎏🎏 LangGraph astream is finished!")
                if self._is_websocket_open(websocket):
                    await websocket.send_json({
                        "type": "end"
                    })

            except CancelledError as e:
                logger.info("handle_request was cancelled")
                # Only send queued message if WebSocket is still open
                if self._is_websocket_open(websocket):
                    await websocket.send_json({
                        "type": "queued",
                        "content": "Message queued!"
                    })
                raise e
            except Exception as e:
                logger.error(f"Error handling request: {str(e)}", exc_info=True)
                # Only send error message if WebSocket is still open
                if self._is_websocket_open(websocket):
                    await websocket.send_json({
                        "type": "error",
                        "content": f"Error processing request: {str(e)}"
                    })
                raise e

    # 08/21/2025: Is this being used at all..?
    async def get_chat_history(self, thread_id: str):
        # For chat history, we don't need a specific agent, just get any workflow to access the checkpointer
        # This is a bit of a hack - we should refactor this to not need the workflow for just getting history
        try:
            app, _, _ = self.get_langgraph_app_and_state({"agent_name": "llamabot", "message": "", "api_token": "", "agent_prompt": ""})
            config = {"configurable": {"thread_id": thread_id}}
            state_history = await app.aget_state(config=config)
            return state_history[0] #gets the actual state.
        except Exception as e:
            logger.error(f"Error getting chat history: {e}")
            return None

    # 08/21/2025: Duplicated code from main.py. Is this getting used?
    def get_or_create_checkpointer(self):
        """Get persistent checkpointer, creating once if needed"""

        if self.app.state.async_checkpointer is not None:
            return self.app.state.async_checkpointer
        
        db_uri = os.getenv("DB_URI")
        self.app.state.async_checkpointer = MemorySaver() # save in RAM if postgres is not available
        if db_uri:
            try:
                # Create connection pool and PostgresSaver directly
                pool = AsyncConnectionPool(db_uri)
                self.app.state.async_checkpointer = AsyncPostgresSaver(pool)
                self.app.state.async_checkpointer.setup()  # Make this async
                logger.info("✅✅✅ Using PostgreSQL persistence!")
            except Exception as e:
                logger.warning(f"Failed to connect to PostgreSQL: {e}. Using MemorySaver.")
        else:
            logger.info("❌❌❌ No DB_URI found. Using MemorySaver for session-based persistence.")
        
        return self.app.state.async_checkpointer

    def cleanup_connection(self, websocket: WebSocket):
        """Clean up resources when a connection is closed"""
        ws_id = id(websocket)
        if ws_id in self.locks:
            del self.locks[ws_id]


    def get_workflow_from_langgraph_json(self, message: dict) -> tuple[str, dict]:
        """
        Return (workflow_path, agent_config) for `message["agent_name"]`.

        workflow_path: e.g. "./agents/llamapress/nodes.py:build_workflow"
        agent_config: dict with optional keys like 'recursion_limit', 'max_messages'
                      Empty dict if no custom config specified.

        Raises FileNotFoundError if the JSON itself can't be located.
        Raises KeyError if the agent isn't present in the JSON.
        """

        agent_name = message.get("agent_name")
        if not agent_name:
            raise KeyError("agent_name missing from message")

        # 1️⃣  explicit override (useful in containers / CI)
        explicit = os.getenv("LANGGRAPH_CONFIG")
        if explicit:
            cfg_path = Path(explicit).expanduser()
            if not cfg_path.is_file():
                raise FileNotFoundError(f"LANGGRAPH_CONFIG='{cfg_path}' not found")
            return self._load_workflow(cfg_path, agent_name)

        # 2️⃣  walk up the tree from the directory that contains *this* file
        here = Path(__file__).resolve().parent
        for parent in [here, *here.parents]:
            candidate = parent / "langgraph.json"
            if candidate.is_file():
                return self._load_workflow(candidate, agent_name)

        # 3️⃣  legacy relative fallbacks (same semantics you had)
        legacy_paths = ["../langgraph.json", "../../langgraph.json", "langgraph.json"]
        for rel in legacy_paths:
            candidate = Path(rel).resolve()
            if candidate.is_file():
                return self._load_workflow(candidate, agent_name)

        raise FileNotFoundError("langgraph.json not found in any expected location")


    def _load_workflow(self, cfg_path: Path, agent_name: str) -> tuple[str, dict]:
        """
        Load JSON at cfg_path and return (workflow_path, agent_config) for agent_name.

        Supports two formats in langgraph.json:
        1. Simple string (backwards compatible):
           "llamabot": "./agents/llamabot/nodes.py:build_workflow"

        2. Object with config (new format):
           "leo": {
               "workflow": "./user_agents/leo/nodes.py:build_workflow",
               "recursion_limit": 200,
               "max_messages": 50
           }

        Returns:
            tuple: (workflow_path, agent_config) where agent_config contains any
                   custom settings like recursion_limit, max_messages, etc.
                   If using simple string format, agent_config will be empty dict.
        """
        with cfg_path.open("r") as f:
            data = json.load(f)

        graphs = data.get("graphs", {})
        if agent_name not in graphs:
            raise KeyError(f"Agent '{agent_name}' not found in {cfg_path}")

        graph_entry = graphs[agent_name]

        # Handle both formats: simple string or object with config
        if isinstance(graph_entry, str):
            # Simple string format (backwards compatible)
            return graph_entry, {}
        elif isinstance(graph_entry, dict):
            # Object format with config
            workflow_path = graph_entry.get("workflow")
            if not workflow_path:
                raise KeyError(f"Agent '{agent_name}' config missing 'workflow' key in {cfg_path}")

            # Extract config (everything except 'workflow')
            agent_config = {k: v for k, v in graph_entry.items() if k != "workflow"}
            return workflow_path, agent_config
        else:
            raise ValueError(f"Invalid format for agent '{agent_name}' in {cfg_path}. Expected string or object.")
    
    def get_langgraph_app_and_state(self, message: dict):
        """
        Returns (app, state, agent_config) tuple.

        agent_config contains optional per-agent settings like:
        - recursion_limit: max graph execution cycles (default: 100)
        - max_messages: message history limit (default: 30)
        """
        app = None
        state = message
        agent_config = {}  # Default empty config

        if message.get("agent_name") is not None:
            langgraph_workflow, agent_config = self.get_workflow_from_langgraph_json(message)
            if langgraph_workflow is not None:
                app = self.get_app_from_workflow_string(langgraph_workflow)

                # Create messages from the message content

                # We removed this timestamp because we don't want to mess with prompt caching. If this date is different every time, it could cause a cache miss for the LLM provider.
                # messages = [HumanMessage(content=message.get("message"), response_metadata={'created_at': datetime.now()})]
                messages = [HumanMessage(content=message.get("message"))]

                # Start with the transformed messages field
                state = {"messages": messages}

                # Pass through ALL fields except the ones used for system routing
                system_routing_fields = {
                    "message",      # We transformed this into messages
                    "agent_name",   # Used for workflow routing only
                    "thread_id"     # Used for LangGraph config only
                }

                # Pass everything else through naturally
                for key, value in message.items():
                    if key not in system_routing_fields:
                        state[key] = value

                logger.info(f"Created state with keys: {list(state.keys())}")
                if agent_config:
                    logger.info(f"Agent config: {agent_config}")

            else:
                raise ValueError(f"Unknown workflow: {message.get('agent_name')}")

        return app, state, agent_config
    
    # This method resolves an agent name to a workflow with the checkpointer.
    # Super important for routing to the right agent workflow for websockets requests.
    def get_app_from_workflow_string(self, workflow_string: str):
        """Get pre-compiled graph from cache (singleton pattern for memory efficiency)"""

        # Extract agent name from workflow_string
        # e.g., "./app/agents/llamabot/nodes.py:build_workflow" → "llamabot"
        # or "./agents/rails_agent/nodes.py:build_workflow" → "rails_agent"
        parts = workflow_string.split('/')
        # Find the agent name (typically second-to-last component)
        agent_name = parts[-2] if len(parts) >= 2 else None

        # Try to get from cache first (compiled at startup)
        if agent_name and hasattr(self.app.state, 'compiled_graphs') and agent_name in self.app.state.compiled_graphs:
            logger.info(f"✅ Using cached compiled graph for agent: {agent_name}")
            return self.app.state.compiled_graphs[agent_name]

        # Fallback: compile on-demand (for backward compatibility or new agents)
        logger.warning(f"⚠️ Compiling graph on-demand for: {agent_name} (not found in cache). Consider adding to startup compilation.")

        # Split the path into module path and function name
        module_path, function_name = workflow_string.split(':')
        # Remove './' if present and convert path to module format
        if module_path.startswith('./'):
            module_path = module_path[2:]
        module_path = module_path.replace('/', '.').replace('.py', '')

        # Dynamically import the module and get the function
        module = importlib.import_module(module_path)
        workflow_builder = getattr(module, function_name)

        # Build the workflow using the imported function
        return workflow_builder(checkpointer=self.get_or_create_checkpointer())