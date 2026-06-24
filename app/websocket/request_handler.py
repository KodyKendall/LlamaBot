import asyncio
from asyncio import Lock, CancelledError

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketState

from app.websocket.web_socket_request_context import WebSocketRequestContext
from app.lib.token_usage import extract_token_usage
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

# Model capabilities for multimodal content live in a shared module so the
# history-scrubbing middleware and this handler never drift apart.
from app.agents.leonardo.model_capabilities import (
    MODEL_CAPABILITIES,
    get_model_capabilities,
    get_file_category,
)

async def _report_tool_messages(*, messages, mothership, thread_id: str, agent_depth: int) -> None:
    """Report ToolMessage observations to the mothership.

    Called from the update-stream branch for every node update. Iterates the
    messages list and fires one report_message call per ToolMessage so the
    mothership can reconstruct full agent trajectories (action → observation).
    Sub-agent tool outputs are included (unlike assistant turns) but tagged
    with agent_depth so they can be filtered downstream.
    """
    from langchain_core.messages import ToolMessage as LCToolMessage
    for msg in messages:
        if not isinstance(msg, LCToolMessage):
            continue
        tool_call_id = getattr(msg, "tool_call_id", None)
        tool_name = getattr(msg, "name", None)
        await mothership.report_message(
            thread_id=thread_id,
            role="tool",
            content=str(msg.content),
            sent_at=datetime.now(timezone.utc).isoformat(),
            model=None,
            tool_call_id=tool_call_id,
            tool_calls=[{
                "name": tool_name,
                "tool_call_id": tool_call_id,
                "agent_depth": agent_depth,
            }],
        )


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

    async def _repair_thread_state_if_needed(self, app, config):
        """
        Detect and repair two shapes of corrupted thread state that violate
        the LLM contract `every tool message must follow an assistant message
        with matching tool_calls`:

        (A) Orphan ToolMessage — a ToolMessage with no preceding AIMessage
            whose tool_calls include its tool_call_id. Cause: prior repair
            or summarization dropped the anchoring AIMessage but left the
            ToolMessage. Fix: RemoveMessage the orphan.

        (B) Dangling tool_calls — an AIMessage with tool_calls that have no
            corresponding following ToolMessage. Cause: task cancelled
            mid-tool-execution. Fix: remove anything after the corrupted
            AIMessage and inject synthetic "[Cancelled]" ToolMessages.

        Both fixes are applied in a single aupdate_state call.
        """
        from langchain_core.messages import AIMessage, ToolMessage as LCToolMessage
        from langchain_core.messages import RemoveMessage

        try:
            state_snapshot = await app.aget_state(config)
            if not state_snapshot or not state_snapshot.values:
                return

            messages = state_snapshot.values.get("messages", [])
            if not messages:
                return

            # Pass 1 (shape A): find orphan ToolMessages. Walk left-to-right
            # tracking the "open" set of tool_call_ids from the most recent
            # AIMessage. A ToolMessage whose tool_call_id is not in that set
            # has no anchor.
            orphan_msgs = []
            open_ids: set = set()
            for msg in messages:
                if isinstance(msg, AIMessage):
                    open_ids = {tc["id"] for tc in (getattr(msg, "tool_calls", []) or [])}
                elif isinstance(msg, LCToolMessage):
                    tcid = getattr(msg, "tool_call_id", None)
                    if tcid in open_ids:
                        open_ids.discard(tcid)
                    else:
                        orphan_msgs.append(msg)

            # Pass 2 (shape B): find first AIMessage with dangling tool_calls.
            corrupted_index = None
            for i, msg in enumerate(messages):
                if not isinstance(msg, AIMessage):
                    continue
                tool_calls = getattr(msg, "tool_calls", [])
                if not tool_calls:
                    continue
                expected_ids = {tc["id"] for tc in tool_calls}
                found_ids = set()
                for j in range(i + 1, len(messages)):
                    if isinstance(messages[j], LCToolMessage):
                        found_ids.add(getattr(messages[j], "tool_call_id", None))
                    elif isinstance(messages[j], AIMessage):
                        break
                if not expected_ids.issubset(found_ids):
                    corrupted_index = i
                    break

            if not orphan_msgs and corrupted_index is None:
                return

            remove_ops = []
            repair_messages = []

            # Shape A: RemoveMessage each orphan ToolMessage. Orphans removed
            # here will not be in the messages_to_remove slice below because
            # the dangling-tool_calls fix only looks after `corrupted_index`.
            if orphan_msgs:
                logger.warning(
                    f"Corrupted thread state detected: {len(orphan_msgs)} orphan ToolMessage(s) "
                    f"with no anchoring AIMessage. Repairing..."
                )
                for m in orphan_msgs:
                    mid = getattr(m, "id", None)
                    if mid:
                        remove_ops.append(RemoveMessage(id=mid))

            # Shape B: drop everything after the corrupted AIMessage and inject
            # synthetic ToolMessages for its dangling tool_calls.
            if corrupted_index is not None:
                corrupted_msg = messages[corrupted_index]
                tool_calls = corrupted_msg.tool_calls
                logger.warning(
                    f"Corrupted thread state detected at message index {corrupted_index}: "
                    f"AIMessage with {len(tool_calls)} dangling tool_call(s). Repairing..."
                )
                for m in messages[corrupted_index + 1:]:
                    if hasattr(m, "id") and m.id:
                        remove_ops.append(RemoveMessage(id=m.id))
                for tc in tool_calls:
                    repair_messages.append(LCToolMessage(
                        content="[Cancelled] Tool execution was interrupted before completion.",
                        tool_call_id=tc["id"],
                        name=tc.get("name", "unknown"),
                    ))

            update_messages = remove_ops + repair_messages
            await app.aupdate_state(config, {"messages": update_messages})
            logger.info(
                f"Thread state repaired: removed {len(remove_ops)} message(s) "
                f"({len(orphan_msgs)} orphan(s) + others), "
                f"injected {len(repair_messages)} synthetic ToolMessage(s)"
            )

        except Exception as e:
            logger.warning(f"Thread state repair check failed (non-fatal): {e}")

    async def _check_paywall_or_block(self, websocket: WebSocket) -> bool:
        """
        Per-instance paywall gate. Returns True if the message should be blocked
        (a paywall_hit was sent to the websocket); False if it should proceed.

        Fast path: cached state says allowed (or cache empty) -> return False, no network.
        Slow path: cached state says blocked -> recheck mothership (the "user just paid"
        recovery path). Fail-open on any mothership error.
        """
        paywall_enabled = os.getenv("PAYWALL_ENABLED", "false").lower() == "true"
        logger.info(f"paywall gate: PAYWALL_ENABLED={paywall_enabled}")
        if not paywall_enabled:
            return False

        credits = getattr(self.app.state, "paywall_credits", {}) or {}
        logger.info(f"paywall gate: cache state={credits}")

        # Cold start (no cache yet) or cached allowed -> fast path, allow.
        if credits.get("allowed_next") is not False:
            logger.info("paywall gate: cache allows (or empty), fast path -> ALLOW")
            return False

        # Cached blocked -> recheck mothership.
        mothership = getattr(self.app.state, "mothership_client", None)
        if mothership is None:
            logger.info("paywall gate: no mothership client -> ALLOW (fail-open)")
            return False

        logger.info("paywall gate: cache blocked, calling check_paywall...")
        recheck = await mothership.check_paywall()
        logger.info(f"paywall gate: recheck returned {recheck}")
        if recheck is None:
            logger.info("paywall gate: recheck failed -> ALLOW (fail-open)")
            return False

        if recheck.get("allowed"):
            self.app.state.paywall_credits = {
                "allowed_next": True,
                "messages_remaining": recheck.get("messages_remaining"),
            }
            logger.info(f"paywall gate: recheck says allowed -> ALLOW, cache updated to {self.app.state.paywall_credits}")
            return False

        # Still blocked — refresh cache and notify frontend.
        messages_remaining = recheck.get("messages_remaining", 0)
        self.app.state.paywall_credits = {
            "allowed_next": False,
            "messages_remaining": messages_remaining,
        }
        logger.info(f"paywall gate: recheck confirms BLOCKED, sending paywall_hit (messages_remaining={messages_remaining})")
        if self._is_websocket_open(websocket):
            await websocket.send_json({
                "type": "paywall_hit",
                "messages_remaining": messages_remaining,
            })
        return True

    async def _handle_compact_command(self, message: dict, websocket: WebSocket) -> None:
        """Stream a context-window compaction through the same WS pipeline as a normal turn.

        Summarises everything older than the last 15 messages using the same model and
        prompt that SummarizationMiddleware uses automatically.  The summary streams back
        as AIMessageChunk events so the frontend shows a thinking shimmer and live text —
        identical to what the user sees when auto-summarization triggers in the middleware.
        After the checkpoint is updated the new (smaller) token count is sent so the
        context wheel refreshes immediately.
        """
        from app.agents.leonardo.llm_factory import make_summarization_model
        from app.agents.leonardo.rails_agent.nodes import SUMMARIZATION_PROMPT
        from langchain.agents.middleware import SummarizationMiddleware
        from langchain_core.messages import RemoveMessage
        from langgraph.graph.message import REMOVE_ALL_MESSAGES
        from langchain_core.messages.utils import get_buffer_string

        thread_id = message.get('thread_id')
        agent_name = message.get('agent_name', 'rails_agent')

        async def _send(data: dict) -> None:
            if self._is_websocket_open(websocket):
                await websocket.send_json(data)

        try:
            graph = (
                self.app.state.compiled_graphs.get(agent_name)
                or self.app.state.compiled_graphs.get('rails_agent')
                or self.app.state.compiled_graphs.get('llamabot')
            )
            if not graph:
                await _send({"type": "AIMessageChunk", "content": "⚠️ No compiled graph available — try again after the server finishes starting."})
                await _send({"type": "end"})
                return

            config = {"configurable": {"thread_id": thread_id}}
            state_snapshot = await graph.aget_state(config=config)
            messages = list(state_snapshot.values.get("messages", []))
            keep_count = 15

            # Kick off the thinking shimmer immediately
            await _send({
                "type": "AIMessageChunk",
                "content": "",
                "thinking": [{"type": "thinking", "thinking": f"Compacting conversation… analysing {len(messages)} messages, keeping the last {keep_count}."}],
            })

            if len(messages) <= keep_count:
                await _send({"type": "AIMessageChunk", "content": f"Nothing to compact — only {len(messages)} messages in context (need more than {keep_count})."})
                await _send({"type": "end"})
                return

            model, token_counter, trim_tokens_to_summarize = make_summarization_model()

            middleware = SummarizationMiddleware(
                model=model,
                trigger=("tokens", 1),
                keep=("messages", keep_count),
                token_counter=token_counter,
                trim_tokens_to_summarize=trim_tokens_to_summarize,
                summary_prompt=SUMMARIZATION_PROMPT,
            )

            middleware._ensure_message_ids(messages)
            cutoff_index = middleware._determine_cutoff_index(messages)

            if cutoff_index <= 0:
                await _send({"type": "AIMessageChunk", "content": "Context is already compact — nothing to summarise."})
                await _send({"type": "end"})
                return

            messages_to_summarize, preserved_messages = middleware._partition_messages(messages, cutoff_index)

            # Build the prompt the same way SummarizationMiddleware does internally
            trimmed = middleware._trim_messages_for_summary(messages_to_summarize)
            formatted = get_buffer_string(trimmed, format="xml")
            prompt = middleware.summary_prompt.format(messages=formatted).rstrip()

            # Stream the summary as AIMessageChunk events
            full_summary = ""
            async for chunk in model.astream(prompt, config={"metadata": {"lc_source": "compact_command"}}):
                raw = chunk.content
                if isinstance(raw, str):
                    text = raw
                elif isinstance(raw, list):
                    text = "".join(
                        b.get("text", "") for b in raw
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    text = ""
                if text:
                    full_summary += text
                    await _send({"type": "AIMessageChunk", "content": text})

            # Persist compacted state to the checkpoint
            new_messages = middleware._build_new_messages(full_summary)
            await graph.aupdate_state(config, {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *new_messages,
                    *preserved_messages,
                ]
            })

            # Recount tokens so the context wheel updates without the user needing to send a message
            try:
                refreshed = await graph.aget_state(config=config)
                new_count = token_counter(list(refreshed.values.get("messages", [])))
            except Exception:
                new_count = 0

            logger.info(
                f"/compact: thread={thread_id} summarised={len(messages_to_summarize)} "
                f"kept={len(preserved_messages)} new_tokens≈{new_count}"
            )

            # Empty content chunk carrying the updated token count — updates the wheel
            await _send({
                "type": "AIMessageChunk",
                "content": "",
                "token_usage": {"input_tokens": new_count, "output_tokens": 0, "total_tokens": new_count},
            })
            await _send({"type": "end"})

        except Exception as e:
            logger.error(f"/compact failed: {e}", exc_info=True)
            await _send({"type": "AIMessageChunk", "content": f"\n\n⚠️ Compact failed: {e}"})
            await _send({"type": "end"})

    # This is a the main function that handles incoming WebSocket requests. This will build the LangGraph workflow, and invoke it from a checkpointed state.
    async def handle_request(self, incoming_message: dict, websocket: WebSocket):
        """Handle incoming WebSocket requests with proper locking and cancellation"""
        ws_id = id(websocket)
        lock = self._get_lock(websocket)

        self.app.state.timestamp = datetime.now(timezone.utc) # keep timestamp updated

        # Paywall gate — must run before report_message so blocked messages
        # don't get counted against the user's quota.
        if await self._check_paywall_or_block(websocket):
            return

        mothership = getattr(self.app.state, "mothership_client", None)
        if mothership is not None:
            async def _report_user_and_cache_paywall():
                result = await mothership.report_message(
                    thread_id=str(incoming_message.get("thread_id", "")),
                    role="user",
                    content=str(incoming_message.get("message", "")),
                    sent_at=datetime.now(timezone.utc).isoformat(),
                )
                # Only role="user" responses carry paywall fields.
                if result and "allowed_next" in result:
                    self.app.state.paywall_credits = {
                        "allowed_next": result.get("allowed_next"),
                        "messages_remaining": result.get("messages_remaining"),
                    }
                    logger.info(f"paywall cache updated: {self.app.state.paywall_credits}")
            asyncio.create_task(_report_user_and_cache_paywall())

        # /compact is a system operation — skip paywall and mothership reporting,
        # acquire the lock so it can't race with an active agent run.
        if incoming_message.get('message', '').strip() == '/compact':
            async with lock:
                await self._handle_compact_command(incoming_message, websocket)
            return

        async with lock:
            try:
                app, state, agent_config = self.get_langgraph_app_and_state(incoming_message)

                # Default limits (can be overridden per-agent in langgraph.json)
                DEFAULT_RECURSION_LIMIT = 450

                # Get agent-specific limits or use defaults
                recursion_limit = agent_config.get("recursion_limit", DEFAULT_RECURSION_LIMIT)

                # Note: Message history management is now handled by SummarizationMiddleware
                # in each agent's middleware stack, which provides intelligent summarization
                # instead of naive message trimming.

                config = {
                    "configurable": {
                        "thread_id": f"{incoming_message.get('thread_id')}",
                        "origin": incoming_message.get('origin', ''),
                        "recursion_limit": recursion_limit
                    },
                    "recursion_limit": recursion_limit
                }

                # Auto-repair corrupted thread state (dangling tool_calls without ToolMessages)
                # This can happen when a previous task was cancelled mid-tool-execution
                await self._repair_thread_state_if_needed(app, config)

                async for chunk in app.astream(state, config=config, stream_mode=["updates", "messages"], subgraphs=True):

                    # If the WS died mid-stream (e.g., uvicorn keepalive ping
                    # timeout), stop here. Continuing would let the graph keep
                    # advancing through tool nodes and writing checkpoints with
                    # no observer, producing orphan ToolMessages that corrupt
                    # the thread.
                    if not self._is_websocket_open(websocket):
                        logger.warning(
                            f"WS closed mid-stream for thread {incoming_message.get('thread_id')}; "
                            f"aborting astream consumer."
                        )
                        if mothership is not None:
                            asyncio.create_task(mothership.report_disconnect(
                                thread_id=str(incoming_message.get("thread_id", "")),
                                reason="ws_closed_mid_stream",
                            ))
                        break

                    # NOTE: In LangGraph 0.5, they introduced this "subgraphs" parameter, that changes the datashape if you set it to True.
                    # if subgraph=True, it returns a tuple with 3 elements, instead of 2 elements.
                    # the first element is the subgraph name, the second element is the streaming data type ["updates", "messages", "values"], and the third element is the actual metadata.

                    is_this_chunk_an_llm_message = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'messages'
                    is_this_chunk_an_update_stream_type = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'updates'
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"🍅🍅🍅 Chunk: {chunk}")
                    
                    # Extract agent depth from subgraph tuple
                    # chunk[0] is a tuple like () for main agent or ('tools:xxx',) for sub-agent
                    subgraph_tuple = chunk[0] if isinstance(chunk, tuple) and len(chunk) >= 1 else ()
                    agent_depth = len(subgraph_tuple)  # 0 = main agent, 1 = sub-agent, 2 = nested sub-agent
                    is_subagent = agent_depth > 0

                    if is_this_chunk_an_llm_message:
                        message_chunk_from_llm = chunk[2][0] #AIMessageChunk object -> https://python.langchain.com/api_reference/core/messages/langchain_core.messages.ai.AIMessageChunk.html
                        data_type = "AIMessageChunk"
                        base_message_as_dict = dumpd(chunk[2][0])["kwargs"]

                        # Content format varies by LLM provider:
                        # - OpenAI: content is a string ("Hello world")
                        # - Anthropic/Claude: content is a list of content blocks [{type: "text", text: "..."}]
                        # - Gemini: content is a list of content blocks [{type: "text", text: "..."}, {type: "image_url", ...}]
                        # - Thinking/Reasoning blocks: [{type: "thinking"/"reasoning", text/thinking: "..."}]
                        # - DeepSeek: reasoning_content in additional_kwargs (separate from content)
                        content = base_message_as_dict["content"]
                        if logger.isEnabledFor(logging.DEBUG):
                            True
                            # logger.debug(f"🍅 Content type: {type(content)}, Content: {content}")

                        # Separate thinking/reasoning content from regular text content
                        # This allows the frontend to display thinking in a dedicated area
                        thinking_content = None
                        text_content = content

                        # Check for DeepSeek reasoning_content in additional_kwargs
                        # DeepSeek returns reasoning as a separate field, not in content blocks
                        additional_kwargs = base_message_as_dict.get("additional_kwargs", {})
                        deepseek_reasoning = additional_kwargs.get("reasoning_content")
                        if deepseek_reasoning:
                            thinking_content = [{"type": "thinking", "thinking": deepseek_reasoning}]
                            # logger.info(f"🧠 DeepSeek reasoning_content: {deepseek_reasoning[:100]}...")

                        if isinstance(content, list):
                            # Extract thinking/reasoning blocks (varies by provider)
                            # - Claude: {type: "thinking", thinking: "..."}
                            # - OpenAI: {type: "reasoning", summary: [...]} or {type: "reasoning_summary", text: "..."}
                            # - Gemini (langchain-google-genai): {type: "thinking", thinking: "...", signature: "..."}
                            thinking_blocks = [
                                b for b in content
                                if isinstance(b, dict) and (
                                    b.get("type") == "reasoning" or
                                    b.get("type") == "reasoning_summary" or
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
                                "base_message": base_message_as_dict,
                                "agent_depth": agent_depth,  # 0 = main agent, 1+ = sub-agent depth
                                "is_subagent": is_subagent,  # True if this is from a delegated sub-agent
                            }
                            if token_usage:
                                ws_message["token_usage"] = token_usage
                            await websocket.send_json(ws_message)

                    elif is_this_chunk_an_update_stream_type: # This means that LangGraph has given us a state update. This will often include a new message from the AI.
                        state_object = chunk[2]
                        # logger.info(f"🧠🧠🧠 LangGraph Output (State Update): {state_object}")

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
                                                # logger.info(f"🔨🔨🔨 Tool Call Name: {tool_call_name}")
                                                # logger.info(f"🔨🔨🔨 Tool Call Args: {tool_call_args}")

                                    # AIMessage is not serializable to JSON, so we need to convert it to a string.
                                    messages_as_string = [msg.content if hasattr(msg, 'content') else str(msg) for msg in messages]

                                    #NOTE: I found we're able to serialize AIMessage into dict using dumpd.
                                    try:
                                        base_message_as_dict = dumpd(message)["kwargs"]
                                    except Exception as e:
                                        logger.warning(f"Failed to serialize message: {e}")
                                        base_message_as_dict = {"content": str(message), "type": "ai"}

                                    # Extract token usage metadata if available (for context window tracking).
                                    # Includes prompt-cache fields (cache_read/cache_creation) so the
                                    # mothership can measure cache-hit rate. Anthropic sends usage_metadata
                                    # on the final AIMessage (not during streaming).
                                    token_usage = extract_token_usage(message)
                                    # logger.info(f"📊 Token usage: {token_usage}")

                                    # Only send if WebSocket is still open
                                    if self._is_websocket_open(websocket):

                                        # NOTE: This JSON object is a standardized format that we've been using for all our front-ends.
                                        # Eventually, we might want to just rely on the base_message data shape as the source of truth for all front-ends.
                                        llamapress_user_interface_json = { # we're forcing this shape to match a BaseMessage type.
                                            "type": message.type if hasattr(message, 'type') else "ai",
                                            "content": messages_as_string[-1] if messages_as_string else "",
                                            "tool_calls": tool_calls,
                                            "base_message": base_message_as_dict,
                                            "agent_depth": agent_depth,  # 0 = main agent, 1+ = sub-agent depth
                                            "is_subagent": is_subagent,  # True if this is from a delegated sub-agent
                                        }
                                        if token_usage:
                                            llamapress_user_interface_json["token_usage"] = token_usage

                                        await websocket.send_json(llamapress_user_interface_json)

                                        if not is_subagent and llamapress_user_interface_json.get("type") == "ai":
                                            mothership = getattr(self.app.state, "mothership_client", None)
                                            if mothership is not None:
                                                model_name = (base_message_as_dict.get("response_metadata") or {}).get("model_name")
                                                # Use message.tool_calls (LangChain normalized {name, args, id} shape)
                                                # rather than additional_kwargs.tool_calls (raw OpenAI shape) so the
                                                # mothership's InstanceMessage#tool_call_names helper can read tc["name"].
                                                normalized_tool_calls = (
                                                    list(message.tool_calls)
                                                    if hasattr(message, "tool_calls") and message.tool_calls
                                                    else None
                                                )
                                                asyncio.create_task(mothership.report_message(
                                                    thread_id=str(incoming_message.get("thread_id", "")),
                                                    role="assistant",
                                                    content=str(llamapress_user_interface_json.get("content", "")),
                                                    sent_at=datetime.now(timezone.utc).isoformat(),
                                                    model=model_name,
                                                    token_usage=token_usage,
                                                    tool_calls=normalized_tool_calls,
                                                ))

                                        # Report ToolMessage observations (tool outputs/observations).
                                        # Unlike assistant turns, sub-agent tool outputs ARE reported
                                        # (agent_depth tag lets mothership filter them). No truncation —
                                        # the mothership receives the raw content verbatim.
                                        mothership = getattr(self.app.state, "mothership_client", None)
                                        if mothership is not None:
                                            asyncio.create_task(_report_tool_messages(
                                                messages=messages,
                                                mothership=mothership,
                                                thread_id=str(incoming_message.get("thread_id", "")),
                                                agent_depth=agent_depth,
                                            ))

                        # logger.info(f"LangGraph Output (State Update): {chunk}")

                        # chunk will look like this:
                        # {'llamabot': {'messages': [AIMessage(content='Hello! I hear you loud and clear. I'm LlamaBot, your full-stack Rails developer assistant. How can I help you today?', additional_kwargs={}, response_metadata={'finish_reason': 'stop', 'model_name': 'o4-mini-2025-04-16', 'service_tier': 'default'}, id='run--ce385bc4-fecb-4127-81d2-1da5814874f8')]}}

                    else:
                        True
                        # logger.info(f"Workflow output: {chunk}")

                print("🎏🎏🎏 LangGraph astream is finished!")

                # Check if graph was interrupted (HITL approval, plan mode question, or mode switch)
                interrupted = await self._check_and_send_interrupts(app, config, incoming_message, websocket)

                if not interrupted:
                    # Update thread metadata after successful message processing
                    await self._update_thread_metadata(incoming_message)

                    # Clean up intermediate checkpoints for THIS thread only after successful run.
                    # SAFETY: this aggressive per-thread trim is only valid for non-DeltaChannel
                    # agents (every checkpoint is a full snapshot). For DeltaChannel agents it would
                    # destroy the delta chain (snapshot + ancestor writes) and corrupt the thread, so
                    # we skip it — DeltaChannel already keeps per-checkpoint storage tiny.
                    if hasattr(self.app.state, 'checkpointer_pool') and self.app.state.checkpointer_pool is not None:
                        try:
                            from app.services.checkpoint_cleanup import (
                                cleanup_thread_checkpoints_except_latest,
                                graph_uses_delta_channel,
                            )
                            thread_id = incoming_message.get('thread_id')
                            if thread_id and not graph_uses_delta_channel(app):
                                await cleanup_thread_checkpoints_except_latest(
                                    self.app.state.checkpointer_pool,
                                    thread_id
                                )
                        except Exception as e:
                            # Non-fatal - don't fail the request if cleanup fails
                            logger.warning(f"Post-run checkpoint cleanup failed (non-fatal): {e}")

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

    async def handle_approval_response(self, response_message: dict, websocket: WebSocket):
        """Resume a graph after user approves/rejects a HITL request."""
        from langgraph.types import Command

        ws_id = id(websocket)
        lock = self._get_lock(websocket)

        async with lock:
            try:
                # Re-resolve the graph (same agent, with HITL)
                app, _, agent_config = self.get_langgraph_app_and_state({
                    "agent_name": response_message.get("agent_name"),
                    "message": "",  # No new message, just resuming
                    "ask_before_edits": True,
                })

                DEFAULT_RECURSION_LIMIT = 450
                recursion_limit = agent_config.get("recursion_limit", DEFAULT_RECURSION_LIMIT)

                config = {
                    "configurable": {
                        "thread_id": f"{response_message.get('thread_id')}",
                        "recursion_limit": recursion_limit
                    },
                    "recursion_limit": recursion_limit
                }

                # Auto-repair corrupted thread state (defensive - less likely in HITL flow)
                await self._repair_thread_state_if_needed(app, config)

                # Build HITLResponse from user decisions
                decisions = response_message.get("decisions", [])
                hitl_response = {"decisions": decisions}

                # Resume the graph
                async for chunk in app.astream(Command(resume=hitl_response), config=config, stream_mode=["updates", "messages"], subgraphs=True):
                    is_this_chunk_an_llm_message = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'messages'
                    is_this_chunk_an_update_stream_type = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'updates'

                    subgraph_tuple = chunk[0] if isinstance(chunk, tuple) and len(chunk) >= 1 else ()
                    agent_depth = len(subgraph_tuple)
                    is_subagent = agent_depth > 0

                    if is_this_chunk_an_llm_message:
                        message_chunk_from_llm = chunk[2][0]
                        base_message_as_dict = dumpd(chunk[2][0])["kwargs"]
                        content = base_message_as_dict["content"]

                        thinking_content = None
                        text_content = content

                        additional_kwargs = base_message_as_dict.get("additional_kwargs", {})
                        deepseek_reasoning = additional_kwargs.get("reasoning_content")
                        if deepseek_reasoning:
                            thinking_content = [{"type": "thinking", "thinking": deepseek_reasoning}]

                        if isinstance(content, list):
                            thinking_blocks = [b for b in content if b.get("type") in ("thinking", "reasoning", "reasoning_summary")]
                            text_blocks = [b for b in content if b.get("type") not in ("thinking", "reasoning", "reasoning_summary")]
                            if thinking_blocks:
                                thinking_content = thinking_blocks
                            text_content = text_blocks if text_blocks else ""

                        if self._is_websocket_open(websocket):
                            ws_message = {
                                "type": "AIMessageChunk",
                                "content": text_content,
                                "thinking": thinking_content,
                                "tool_calls": [],
                                "base_message": base_message_as_dict,
                                "agent_depth": agent_depth,
                                "is_subagent": is_subagent,
                            }
                            await websocket.send_json(ws_message)

                    elif is_this_chunk_an_update_stream_type:
                        state_object = chunk[2]
                        for agent_key, agent_data in state_object.items():
                            if isinstance(agent_data, dict) and 'messages' in agent_data:
                                messages = agent_data['messages']
                                tool_calls = []
                                if messages and len(messages) > 0:
                                    message = messages[-1]
                                    if hasattr(message, 'additional_kwargs') and message.additional_kwargs:
                                        tool_calls_data = message.additional_kwargs.get('tool_calls')
                                        if tool_calls_data:
                                            tool_calls = tool_calls_data

                                    messages_as_string = [msg.content if hasattr(msg, 'content') else str(msg) for msg in messages]
                                    try:
                                        base_message_as_dict = dumpd(message)["kwargs"]
                                    except Exception:
                                        base_message_as_dict = {"content": str(message), "type": "ai"}

                                    token_usage = extract_token_usage(message)

                                    if self._is_websocket_open(websocket):
                                        ws_msg = {
                                            "type": message.type if hasattr(message, 'type') else "ai",
                                            "content": messages_as_string[-1] if messages_as_string else "",
                                            "tool_calls": tool_calls,
                                            "base_message": base_message_as_dict,
                                            "agent_depth": agent_depth,
                                            "is_subagent": is_subagent,
                                        }
                                        if token_usage:
                                            ws_msg["token_usage"] = token_usage
                                        await websocket.send_json(ws_msg)

                # Check for another interrupt (agent may call another destructive tool or ask another question)
                interrupted = await self._check_and_send_interrupts(app, config, response_message, websocket)

                if not interrupted and self._is_websocket_open(websocket):
                    await websocket.send_json({"type": "end"})

            except CancelledError as e:
                logger.info("handle_approval_response was cancelled")
                raise e
            except Exception as e:
                logger.error(f"Error handling approval response: {str(e)}", exc_info=True)
                if self._is_websocket_open(websocket):
                    await websocket.send_json({
                        "type": "error",
                        "content": f"Error resuming after approval: {str(e)}"
                    })
                raise e

    async def _check_and_send_interrupts(self, app, config, message_data, websocket) -> bool:
        """Check for pending interrupts and send appropriate WebSocket messages.

        Returns True if an interrupt was found and sent to the frontend.
        Handles question interrupts, mode switch suggestions, and HITL approval requests.
        """
        try:
            state_snapshot = await app.aget_state(config)
            if not state_snapshot.tasks or not any(t.interrupts for t in state_snapshot.tasks):
                return False

            for task in state_snapshot.tasks:
                for intr in task.interrupts:
                    interrupt_value = intr.value
                    if not self._is_websocket_open(websocket):
                        continue

                    # Plan mode question interrupt
                    if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "user_question":
                        await websocket.send_json({
                            "type": "question_request",
                            "question": interrupt_value.get("question", ""),
                            "options": interrupt_value.get("options", []),
                            "context": interrupt_value.get("context", ""),
                            "thread_id": message_data.get('thread_id'),
                            "agent_name": message_data.get('agent_name'),
                        })
                        logger.info("Graph interrupted for plan mode question")

                    # Suggest plan mode interrupt
                    elif isinstance(interrupt_value, dict) and interrupt_value.get("type") == "suggest_mode_switch":
                        await websocket.send_json({
                            "type": "suggest_mode_switch",
                            "target_mode": interrupt_value.get("target_mode", "plan"),
                            "reason": interrupt_value.get("reason", ""),
                            "original_message": message_data.get('message', ''),
                            "thread_id": message_data.get('thread_id'),
                            "agent_name": message_data.get('agent_name'),
                        })
                        logger.info("Graph interrupted for mode switch suggestion")

                    # Implement ticket interrupt - offer to switch to engineer mode
                    elif isinstance(interrupt_value, dict) and interrupt_value.get("type") == "implement_ticket":
                        await websocket.send_json({
                            "type": "implement_ticket",
                            "ticket_id": interrupt_value.get("ticket_id"),
                            "ticket_title": interrupt_value.get("ticket_title", ""),
                            "ticket_content": interrupt_value.get("ticket_content", ""),
                            "thread_id": message_data.get('thread_id'),
                            "agent_name": message_data.get('agent_name'),
                        })
                        logger.info("Graph interrupted for implement ticket offer")

                    # HITL approval interrupt
                    elif isinstance(interrupt_value, dict) and "action_requests" in interrupt_value:
                        await websocket.send_json({
                            "type": "approval_request",
                            "action_requests": [
                                {"name": ar["name"], "args": ar["args"], "description": ar.get("description", "")}
                                for ar in interrupt_value["action_requests"]
                            ],
                            "thread_id": message_data.get('thread_id'),
                            "agent_name": message_data.get('agent_name'),
                        })
                        logger.info("Graph interrupted for HITL approval")

            return True
        except Exception as e:
            logger.warning(f"Failed to check for interrupts: {e}")
            return False

    async def handle_question_response(self, response_message: dict, websocket: WebSocket):
        """Resume a graph after user answers a plan mode question."""
        from langgraph.types import Command

        ws_id = id(websocket)
        lock = self._get_lock(websocket)

        async with lock:
            try:
                # Re-resolve the graph (same agent, no HITL flag needed)
                app, _, agent_config = self.get_langgraph_app_and_state({
                    "agent_name": response_message.get("agent_name"),
                    "message": "",  # No new message, just resuming
                })

                DEFAULT_RECURSION_LIMIT = 450
                recursion_limit = agent_config.get("recursion_limit", DEFAULT_RECURSION_LIMIT)

                config = {
                    "configurable": {
                        "thread_id": f"{response_message.get('thread_id')}",
                        "recursion_limit": recursion_limit
                    },
                    "recursion_limit": recursion_limit
                }

                # Auto-repair corrupted thread state
                await self._repair_thread_state_if_needed(app, config)

                # Resume the graph — interrupt() returns this answer string
                answer = response_message.get("answer", "")

                async for chunk in app.astream(Command(resume=answer), config=config, stream_mode=["updates", "messages"], subgraphs=True):
                    is_this_chunk_an_llm_message = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'messages'
                    is_this_chunk_an_update_stream_type = isinstance(chunk, tuple) and len(chunk) == 3 and chunk[1] == 'updates'

                    subgraph_tuple = chunk[0] if isinstance(chunk, tuple) and len(chunk) >= 1 else ()
                    agent_depth = len(subgraph_tuple)
                    is_subagent = agent_depth > 0

                    if is_this_chunk_an_llm_message:
                        message_chunk_from_llm = chunk[2][0]
                        base_message_as_dict = dumpd(chunk[2][0])["kwargs"]
                        content = base_message_as_dict["content"]

                        thinking_content = None
                        text_content = content

                        additional_kwargs = base_message_as_dict.get("additional_kwargs", {})
                        deepseek_reasoning = additional_kwargs.get("reasoning_content")
                        if deepseek_reasoning:
                            thinking_content = [{"type": "thinking", "thinking": deepseek_reasoning}]

                        if isinstance(content, list):
                            thinking_blocks = [b for b in content if b.get("type") in ("thinking", "reasoning", "reasoning_summary")]
                            text_blocks = [b for b in content if b.get("type") not in ("thinking", "reasoning", "reasoning_summary")]
                            if thinking_blocks:
                                thinking_content = thinking_blocks
                            text_content = text_blocks if text_blocks else ""

                        if self._is_websocket_open(websocket):
                            ws_message = {
                                "type": "AIMessageChunk",
                                "content": text_content,
                                "thinking": thinking_content,
                                "tool_calls": [],
                                "base_message": base_message_as_dict,
                                "agent_depth": agent_depth,
                                "is_subagent": is_subagent,
                            }
                            await websocket.send_json(ws_message)

                    elif is_this_chunk_an_update_stream_type:
                        state_object = chunk[2]
                        for agent_key, agent_data in state_object.items():
                            if isinstance(agent_data, dict) and 'messages' in agent_data:
                                messages = agent_data['messages']
                                tool_calls = []
                                if messages and len(messages) > 0:
                                    message = messages[-1]
                                    if hasattr(message, 'additional_kwargs') and message.additional_kwargs:
                                        tool_calls_data = message.additional_kwargs.get('tool_calls')
                                        if tool_calls_data:
                                            tool_calls = tool_calls_data

                                    messages_as_string = [msg.content if hasattr(msg, 'content') else str(msg) for msg in messages]
                                    try:
                                        base_message_as_dict = dumpd(message)["kwargs"]
                                    except Exception:
                                        base_message_as_dict = {"content": str(message), "type": "ai"}

                                    token_usage = extract_token_usage(message)

                                    if self._is_websocket_open(websocket):
                                        ws_msg = {
                                            "type": message.type if hasattr(message, 'type') else "ai",
                                            "content": messages_as_string[-1] if messages_as_string else "",
                                            "tool_calls": tool_calls,
                                            "base_message": base_message_as_dict,
                                            "agent_depth": agent_depth,
                                            "is_subagent": is_subagent,
                                        }
                                        if token_usage:
                                            ws_msg["token_usage"] = token_usage
                                        await websocket.send_json(ws_msg)

                # Check for another interrupt (agent may ask another question)
                interrupted = await self._check_and_send_interrupts(app, config, response_message, websocket)

                if not interrupted and self._is_websocket_open(websocket):
                    await websocket.send_json({"type": "end"})

            except CancelledError as e:
                logger.info("handle_question_response was cancelled")
                raise e
            except Exception as e:
                logger.error(f"Error handling question response: {str(e)}", exc_info=True)
                if self._is_websocket_open(websocket):
                    await websocket.send_json({
                        "type": "error",
                        "content": f"Error resuming after question: {str(e)}"
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
    # 02/24/2026: Fixed to use same fallback chain as main.py (AUTH_DB_URI > DB_URI)
    def get_or_create_checkpointer(self):
        """Get persistent checkpointer, creating once if needed.

        IMPORTANT: This should return the checkpointer from app.state that was
        created in main.py startup. Only creates a new one if app.state doesn't have one.
        """
        if self.app.state.async_checkpointer is not None:
            return self.app.state.async_checkpointer

        # Use same fallback chain as main.py (defaults to llamabot_production)
        db_uri = (
            os.getenv("CHECKPOINTER_DB_URI") or
            os.getenv("LEONARDO_DB_URI") or
            os.getenv("AUTH_DB_URI") or
            os.getenv("DB_URI")  # Legacy fallback
        )
        self.app.state.async_checkpointer = MemorySaver()  # save in RAM if postgres is not available
        if db_uri and db_uri.strip():
            try:
                # Create connection pool and PostgresSaver directly
                pool = AsyncConnectionPool(db_uri)
                self.app.state.async_checkpointer = AsyncPostgresSaver(pool)
                self.app.state.checkpointer_pool = pool  # Store pool for checkpoint cleanup service
                # NOTE: Don't call setup() here - tables are created by init_pg_checkpointer.py at startup
                logger.info(f"✅✅✅ Using PostgreSQL persistence (fallback creation)")
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

    async def _update_thread_metadata(self, message: dict):
        """Create or update thread metadata after a successful message exchange.

        This tracks thread metadata for fast sidebar loading without loading
        full LangGraph checkpoint states.
        """
        try:
            from app.db import engine
            from sqlmodel import Session
            from app.models import ThreadMetadata
            from app.services.thread_service import (
                get_or_create_thread_metadata,
                update_thread_metadata,
                schedule_title_generation
            )

            if engine is None:
                logger.debug("Database engine not available, skipping thread metadata update")
                return

            thread_id = message.get('thread_id')
            if not thread_id:
                return

            first_message = message.get('message', '')
            agent_name = message.get('agent_name')

            with Session(engine) as db_session:
                existing = db_session.get(ThreadMetadata, thread_id)
                if existing:
                    # Existing thread: increment message count by 2 (user + AI message)
                    update_thread_metadata(db_session, thread_id, increment_messages=2)
                    logger.debug(f"Updated thread metadata for {thread_id}")
                else:
                    # New thread: create metadata entry with truncated title as placeholder
                    get_or_create_thread_metadata(
                        db_session,
                        thread_id=thread_id,
                        first_message_content=first_message,
                        agent_name=agent_name
                    )
                    logger.info(f"Created thread metadata for {thread_id}")

                    # Schedule async LLM title generation in background (fire-and-forget)
                    # This is completely optional - if it fails, truncated title is already saved
                    if first_message:
                        try:
                            import asyncio
                            task = asyncio.create_task(schedule_title_generation(thread_id, first_message))
                            # Add a callback to log any unexpected errors (should never happen)
                            task.add_done_callback(
                                lambda t: logger.debug(f"Title generation task completed: {t.exception() if t.exception() else 'success'}")
                                if t.done() else None
                            )
                        except Exception as task_error:
                            # Even task creation failure should not affect the main flow
                            logger.debug(f"Failed to schedule title generation (non-critical): {task_error}")
        except Exception as e:
            # Don't fail the request if metadata update fails
            logger.warning(f"Failed to update thread metadata: {e}")

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
    
    def _build_message_content(self, message: dict) -> list | str:
        """
        Build multimodal content array for HumanMessage.

        Now model-aware: checks if the selected LLM supports each file type.
        - Gemini: Supports images, video, PDFs
        - Claude: Supports images and PDFs only (no video)
        - GPT: Supports images only
        - DeepSeek: Text only

        If there are no attachments, returns the plain text string for backwards compatibility.
        If there are attachments, returns a list of content blocks in LangChain format.
        For unsupported file types, adds a text note instead of the binary content.
        """
        text = message.get("message", "")
        attachments = message.get("attachments", [])
        llm_model = message.get("llm_model", "gemini-3-flash")

        # If no attachments, return plain string for backwards compatibility
        if not attachments:
            return text

        # Get model capabilities
        capabilities = get_model_capabilities(llm_model)

        # Build multimodal content array
        content = []
        unsupported_files = []

        # Add text content first
        if text:
            content.append({"type": "text", "text": text})

        # Add file attachments based on model capabilities
        for attachment in attachments:
            mime_type = attachment.get("mime_type")
            data = attachment.get("data")
            filename = attachment.get("filename", "unknown")

            if not mime_type or not data:
                continue

            file_category = get_file_category(mime_type)

            # Check if model supports this file type
            if capabilities.get(file_category, False):
                # Model supports this type - add the content block
                if mime_type.startswith('image/'):
                    # Use image_url format for images (widely supported)
                    content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{data}"
                        }
                    })
                else:
                    # Use file format for PDFs, videos
                    content.append({
                        "type": "file",
                        "source_type": "base64",
                        "mime_type": mime_type,
                        "data": data
                    })
                logger.info(f"Added file attachment: {filename} ({mime_type})")
            else:
                # Model doesn't support this type - track for user notification
                unsupported_files.append(f"{filename} ({mime_type})")
                logger.warning(f"Model {llm_model} doesn't support {mime_type}, skipping: {filename}")

        # If there were unsupported files, add a note to the text content
        if unsupported_files:
            note = f"\n\n[Note: The following attachments were not sent because {llm_model} doesn't support them: {', '.join(unsupported_files)}. Consider using Gemini for video support.]"
            if content and content[0].get("type") == "text":
                content[0]["text"] += note
            else:
                content.insert(0, {"type": "text", "text": note.strip()})

        return content

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
                ask_before_edits = message.get("ask_before_edits", False)
                app = self.get_app_from_workflow_string(langgraph_workflow, ask_before_edits=ask_before_edits)

                # Create messages from the message content (with optional multimodal attachments)
                # We removed this timestamp because we don't want to mess with prompt caching. If this date is different every time, it could cause a cache miss for the LLM provider.
                message_content = self._build_message_content(message)
                messages = [HumanMessage(content=message_content)]

                # Start with the transformed messages field
                state = {"messages": messages}

                # Pass through ALL fields except the ones used for system routing
                system_routing_fields = {
                    "message",      # We transformed this into messages
                    "agent_name",   # Used for workflow routing only
                    "thread_id",    # Used for LangGraph config only
                    "attachments"   # We transformed this into message content blocks
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
    def get_app_from_workflow_string(self, workflow_string: str, ask_before_edits: bool = False):
        """Get pre-compiled graph from cache (singleton pattern for memory efficiency)"""

        # Extract agent name from workflow_string
        # e.g., "./app/agents/llamabot/nodes.py:build_workflow" → "llamabot"
        # or "./agents/rails_agent/nodes.py:build_workflow" → "rails_agent"
        parts = workflow_string.split('/')
        # Find the agent name (typically second-to-last component)
        agent_name = parts[-2] if len(parts) >= 2 else None

        # Use separate cache key for HITL variant
        cache_key = f"{agent_name}:hitl" if ask_before_edits else agent_name

        # Try to get from cache first (compiled at startup)
        if cache_key and hasattr(self.app.state, 'compiled_graphs') and cache_key in self.app.state.compiled_graphs:
            logger.info(f"✅ Using cached compiled graph for agent: {cache_key}")
            return self.app.state.compiled_graphs[cache_key]

        # Fallback: compile on-demand (for backward compatibility or new agents)
        logger.warning(f"⚠️ Compiling graph on-demand for: {cache_key} (not found in cache). Consider adding to startup compilation.")

        # Split the path into module path and function name
        module_path, function_name = workflow_string.split(':')
        # Remove './' if present and convert path to module format
        if module_path.startswith('./'):
            module_path = module_path[2:]
        # Strip the .py extension as a suffix only — using replace('.py', '') would
        # also clobber it inside path segments like 'pyxl_agent' (-> 'xl_agent').
        if module_path.endswith('.py'):
            module_path = module_path[:-3]
        module_path = module_path.replace('/', '.')

        # Dynamically import the module and get the function
        module = importlib.import_module(module_path)
        workflow_builder = getattr(module, function_name)

        # Build the workflow, passing ask_before_edits if the builder supports it
        import inspect
        sig = inspect.signature(workflow_builder)
        if 'ask_before_edits' in sig.parameters:
            compiled = workflow_builder(checkpointer=self.get_or_create_checkpointer(), ask_before_edits=ask_before_edits)
        else:
            compiled = workflow_builder(checkpointer=self.get_or_create_checkpointer())

        # Cache the HITL variant for reuse
        if cache_key and hasattr(self.app.state, 'compiled_graphs'):
            self.app.state.compiled_graphs[cache_key] = compiled

        return compiled