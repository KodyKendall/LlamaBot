import os
import json
import httpx
import logging

logger = logging.getLogger(__name__)
LLAMAPRESS_API_URL = os.getenv("LLAMAPRESS_API_URL")

async def make_api_request_to_llamapress(
    method: str,
    endpoint: str,
    api_token: str,
    payload: dict = None,
    params: dict = None,
):
    """
    Helper function to make authenticated API requests to the LlamaPress Rails API.
    """
    if not LLAMAPRESS_API_URL:
        return "Error: LLAMAPRESS_API_URL environment variable is not set."

    if not api_token:
        return "Error: api_token is required but not provided."

    API_ENDPOINT = f"{LLAMAPRESS_API_URL}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"LlamaBot {api_token}",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                API_ENDPOINT,
                headers=headers,
                json=payload,
                params=params,
                timeout=30,
            )

        if 200 <= response.status_code < 300:
            return response.json()
        else:
            return f"HTTP Error {response.status_code}: {response.text}"

    except httpx.ConnectError:
        return "Error: Could not connect to Rails server. Make sure your Rails app is running."
    except httpx.TimeoutException:
        return "Error: Request timed out. The Rails request may be taking too long to execute."
    except httpx.RequestError as e:
        return f"Request Error: {str(e)}"
    except json.JSONDecodeError:
        return f"Error: Invalid JSON response from server. Raw response: {response.text}"
    except Exception as e:
        return f"Unexpected Error: {str(e)}"