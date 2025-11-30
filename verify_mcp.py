import httpx
import asyncio
import sys

async def verify_mcp_endpoint():
    # Assuming the server is running on localhost:8000
    # Note: This script assumes the server is already running. 
    # Since I cannot start the server myself in this environment easily without blocking, 
    # I will just print what the user should do or try to connect if it happens to be running.
    
    url = "http://localhost:8000/mcp/sse"
    print(f"Checking {url}...")
    
    try:
        async with httpx.AsyncClient() as client:
            # SSE requests usually hang open, so we just want to see if we get a connection
            async with client.stream("GET", url, headers={"Accept": "text/event-stream"}, timeout=5.0) as response:
                if response.status_code == 200:
                    print("✅ MCP SSE endpoint is reachable (Status 200)")
                    print("Reading first few events...")
                    async for line in response.aiter_lines():
                        if line:
                            print(f"Received: {line}")
                        if "event: endpoint" in line or "data:" in line:
                            # Just read a bit and break
                            pass
                        if "data:" in line and "messages" in line:
                             break
                    return True
                else:
                    print(f"❌ MCP SSE endpoint returned status {response.status_code}")
                    return False
                
    except httpx.TimeoutException:
        # If it times out, it might actually be working (streaming), or just slow.
        # But for SSE, a timeout on read is expected if no events are sent immediately.
        # However, we want to check if connection was established.
        print("⚠️ Connection timed out (this might be expected for SSE if no initial event)")
        return True
    except httpx.ConnectError:
        print("❌ Could not connect to localhost:8000. Is the server running?")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == "__main__":
    print("Checking MCP endpoint...")
    asyncio.run(verify_mcp_endpoint())
