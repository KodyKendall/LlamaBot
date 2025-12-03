import asyncio
import sys
import os
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp import ClientSession, StdioServerTransport

# Since we want to expose the REMOTE SSE server to the LOCAL Stdio client (Claude),
# we need to act as a Stdio SERVER that proxies to the SSE CLIENT.
# However, the `mcp` library doesn't have a built-in "proxy" function easily exposed.
#
# A simpler approach for the user might be to use the `mcp-cli` if available, 
# but writing a custom script using `mcp` library primitives is safer.
#
# Actually, `FastMCP` has a `run_stdio_server` but that runs the server logic locally.
# We want to connect to a remote server.
#
# Let's try a minimal implementation using `mcp` low-level primitives.
# We need to read from stdin, parse JSON-RPC, forward to SSE session, get response, write to stdout.
#
# BUT, `ClientSession` is a high-level client. 
#
# Let's look at `mcp` library capabilities.
# If we cannot easily bridge, we might advise the user to use the `mcp` CLI tool if it supports this.
# `npx -y @modelcontextprotocol/inspector` is a client.
#
# Let's try to construct a bridge.
#
# NOTE: This is a non-trivial script to write from scratch without reference.
# However, `mcp` python SDK is designed for this.
#
# Let's try a different approach:
# The user wants to "test it with Claude Desktop".
# Claude Desktop config:
# {
#   "mcpServers": {
#     "fastapi-app": {
#       "command": "python",
#       "args": ["mcp_bridge.py"]
#     }
#   }
# }
#
# `mcp_bridge.py` needs to speak JSON-RPC on stdio.
#
# Implementation:
# 1. Connect to SSE.
# 2. Receive JSON-RPC messages from Stdin.
# 3. Send them to SSE.
# 4. Receive responses from SSE.
# 5. Send them to Stdout.
#
# This is basically a transport bridge.

import sys
import asyncio
import json
import httpx
from sse_starlette.sse import EventSourceResponse # Not needed for client

# We will use a simple implementation that relies on the fact that MCP over SSE 
# uses POST for client->server and SSE for server->client.

async def sse_client_bridge():
    url = "http://localhost:8000/mcp/sse"
    post_url = "http://localhost:8000/mcp/messages"
    
    # We need to capture stdin/stdout
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    w_transport, w_protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(w_transport, w_protocol, reader, loop)

    async with httpx.AsyncClient() as client:
        # 1. Connect to SSE
        async with client.stream("GET", url, headers={"Accept": "text/event-stream"}) as response:
            # We need to handle incoming SSE events and outgoing Stdin messages concurrently
            
            async def handle_incoming_sse():
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        try:
                            message = json.loads(data)
                            # Forward to stdout
                            # MCP stdio transport uses JSON-RPC messages separated by newlines
                            sys.stdout.write(json.dumps(message) + "\n")
                            sys.stdout.flush()
                        except:
                            pass
                    elif line.startswith("event: endpoint"):
                        # Handle endpoint event if needed, but usually just data
                        pass

            async def handle_stdin():
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        message = json.loads(line)
                        # Forward to POST endpoint
                        await client.post(post_url, json=message)
                    except Exception as e:
                        # logger.error(f"Error parsing stdin: {e}")
                        pass

            # Run both
            await asyncio.gather(handle_incoming_sse(), handle_stdin())

# The above is a bit naive because `mcp` protocol has specific handshake.
# However, the `mcp` library should handle the session if we use it.
# But bridging `ClientSession` to `StdioServer` is hard.
#
# BETTER IDEA:
# Since the user has the code locally, why not just run the MCP server logic LOCALLY via stdio?
# The user asked "I want it to work through HTTP SSE".
# So they specifically want to test the SSE path.
#
# Let's use the `mcp` library's `sse_client` to connect, and then we need to expose it.
#
# Actually, there is a simpler way.
# We can use the `mcp` CLI to bridge? No.
#
# Let's stick to the naive bridge but make it robust enough.
# MCP JSON-RPC messages are just JSON objects.
# Stdio transport: one JSON object per line.
# SSE transport: 
#   - Client -> Server: POST JSON object
#   - Server -> Client: SSE "message" event with data=JSON object
#
# So the naive bridge above is actually close to correct, except for headers/session ID.
#
# When connecting to SSE, the server might send a session ID?
# FastMCP/MCP spec:
# GET /sse -> returns text/event-stream.
# First event: "endpoint" event? Or just data?
#
# Let's look at `mcp` spec or implementation.
# FastMCP implementation:
# It uses `sse_starlette`.
#
# Let's write a script that uses `mcp` library to be safe.
# But `mcp` library doesn't have a "Stdio to SSE Bridge" class.
#
# Let's try to use the `mcp` library's `sse_client` context manager.
# It yields a `ClientSession`.
# We want to drive this `ClientSession` from Stdio.
#
# This is actually what `mcp-proxy` would do.
#
# Let's provide a script that uses `httpx` and `asyncio` to bridge.
# It is the most direct way to satisfy "test it with Claude Desktop" + "HTTP SSE".

import argparse

async def main():
    # URL of the SSE endpoint
    sse_url = "http://localhost:8000/mcp/sse"
    post_url = "http://localhost:8000/mcp/messages"

    async with httpx.AsyncClient() as client:
        # Start SSE connection
        async with client.stream("GET", sse_url) as response:
            
            # Task to read from stdin and POST to server
            async def read_stdin():
                loop = asyncio.get_event_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, sys.stdin)
                
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        # Parse just to ensure it's valid JSON, but send raw bytes or dict?
                        # MCP spec: POST body is the JSON-RPC message.
                        msg = json.loads(line)
                        
                        # We might need to inject session ID if the server requires it?
                        # FastMCP handles session via query param or cookie? 
                        # Usually the SSE connection establishes the session.
                        # The POST request needs to be associated with that session.
                        # FastMCP uses `?session_id=...` in the POST URL?
                        #
                        # Let's check how `mcp` library does it.
                        # It usually sends the session ID in the query string of the POST request.
                        # The session ID is received in the first SSE event.
                        
                        # So we need to wait for the session ID.
                        pass 
                    except:
                        continue

            # This is getting complicated to implement correctly.
            #
            # ALTERNATIVE:
            # Just tell the user to use `npx -y @modelcontextprotocol/inspector` to test it?
            # Or `npx -y @modelcontextprotocol/server-sse-client`? (doesn't exist)
            
            pass

# RESTART:
# I will use the `mcp` library.
# I will create a script `mcp_bridge.py` that imports `mcp`.
# But `mcp` library is complex.
#
# Let's try to find a simpler solution.
# The user wants to test with Claude Desktop.
#
# Maybe I can just give them a script that runs the server in stdio mode?
# "Is it possible for me to run this and test it with Claude Desktop?"
# "I want it to work through HTTP SSE".
#
# If I give them a script that runs in stdio mode, it DOES NOT use SSE.
# So I MUST bridge.
#
# Let's try to write a robust bridge using `httpx`.

import json
import sys
import asyncio
import httpx

async def run_bridge():
    base_url = "http://localhost:8000/mcp"
    sse_url = f"{base_url}/sse"
    messages_url = f"{base_url}/messages"

    async with httpx.AsyncClient() as client:
        async with client.stream("GET", sse_url) as response:
            # 1. Read the first event to get the endpoint/session info?
            # FastMCP implementation details:
            # It yields `event: endpoint` with data being the URL for messages?
            # Let's assume standard MCP SSE behavior.
            
            session_id = None
            
            # We need a queue for stdin messages
            stdin_queue = asyncio.Queue()
            
            async def read_stdin():
                loop = asyncio.get_event_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, sys.stdin)
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    await stdin_queue.put(line)

            async def handle_sse():
                nonlocal messages_url
                async for line in response.aiter_lines():
                    if line.startswith("event: endpoint"):
                        # Next line should be data: ...
                        continue
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if not data:
                            continue
                        
                        # Check if it's the endpoint URL
                        # Spec says: event: endpoint \n data: /mcp/messages?session_id=...
                        if data.startswith("/") or data.startswith("http"):
                             # It's the relative or absolute URL for posting messages
                             if data.startswith("/"):
                                 messages_url = f"http://localhost:8000{data}"
                             else:
                                 messages_url = data
                             continue

                        try:
                            # It's a JSON-RPC message
                            msg = json.loads(data)
                            sys.stdout.write(json.dumps(msg) + "\n")
                            sys.stdout.flush()
                        except:
                            pass

            async def handle_stdin_processing():
                while True:
                    line = await stdin_queue.get()
                    try:
                        msg = json.loads(line)
                        # Post to the messages URL (which might have been updated with session_id)
                        await client.post(messages_url, json=msg)
                    except Exception as e:
                        sys.stderr.write(f"Error sending: {e}\n")

            # Run all tasks
            await asyncio.gather(
                read_stdin(),
                handle_sse(),
                handle_stdin_processing()
            )

if __name__ == "__main__":
    try:
        asyncio.run(run_bridge())
    except KeyboardInterrupt:
        pass
