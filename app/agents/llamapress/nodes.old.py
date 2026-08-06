from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from dotenv import load_dotenv
from functools import partial
from typing import Optional
import os
import logging
import requests
import json
from typing import Annotated
from datetime import datetime
import httpx

from .helpers import reassemble_fragments

load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode, InjectedState


logger = logging.getLogger(__name__)

# Warning: Brittle - None type will break this when it's injected into the state for the tool call, and it silently fails. So if it doesn't map state types properly from the frontend, it will break. (must be exactly what's defined here).
class LlamaPressState(MessagesState):
    api_token: str
    agent_prompt: str
    page_id: str
    current_page_html: str
    selected_element: Optional[str]
    javascript_console_errors: Optional[str]
    created_at: Optional[datetime] = datetime.now()


# Tools
@tool
async def write_html_page(
    full_html_document: str,
    message_to_user: str,
    internal_thoughts: str,
    state: Annotated[dict, InjectedState],
) -> str:
    """
    Write an HTML page to the filesystem.
    full_html_document is the full HTML document to write to the filesystem, including CSS and JavaScript.
    message_to_user is a string to tell the user what you're doing.
    internal_thoughts are your thoughts about the command.
    """
    # Debug logging
    logger.info(f"Page ID: {state.get('page_id')}")
    logger.info(
        f"State keys: {list(state.keys()) if isinstance(state, dict) else 'Not a dict'}"
    )

    # Configuration
    LLAMAPRESS_API_URL = os.getenv("LLAMAPRESS_API_URL")

    logger.info(f"Writing HTML page to filesystem! to {LLAMAPRESS_API_URL}")

    # Get page_id from state, with fallback
    page_id = state.get("page_id")
    if not page_id:
        return "Error: page_id is required but not provided in state"

    API_ENDPOINT = f"{LLAMAPRESS_API_URL}/pages/{page_id}.json"
    try:
        # Get API token from state
        api_token = state.get("api_token")
        if not api_token:
            return "Error: api_token is required but not provided in state"

        async with httpx.AsyncClient() as client:
            response = await client.put(
                API_ENDPOINT,
                json={"content": full_html_document},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"LlamaBot {api_token}",
                },
                timeout=30,  # 30 second timeout
            )

        # Parse the response
        if response.status_code == 200:
            data = response.json()
            return json.dumps(data, ensure_ascii=False, indent=2)
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


@tool
def get_weather(city: str, state: Annotated[dict, InjectedState]) -> str:
    """
    Get the weather for a given city.
    city is the city to get the weather for.
    """
    return "Current weather is windy, but sunny and 68 degrees."


@tool
async def handle_selected_element(new_html_code: str, state: LlamaPressState) -> str:
    """Writes new HTML code to the current page.

    This tool is used to modify an existing HTML element on the page.

    Args:
        new_html_code (str): The new HTML code to write. This will replace the selected element.
        state (LlamaBotState): The current state of the LlamaBot.

    Returns:
        str: A success or error message.
    """
    logger.info(f"HTML Snippet code written for the selected element: {new_html_code}")
    reassembled_html = reassemble_fragments(
        new_html_code, state.get("current_page_html")
    )
    api_token = state.get("api_token")
    LLAMAPRESS_API_URL = os.getenv("LLAMAPRESS_API_URL")
    POST_API_ENDPOINT = f"{LLAMAPRESS_API_URL}/pages/{state.get("page_id")}.json"

    async with httpx.AsyncClient() as client:
        response = await client.put(
            POST_API_ENDPOINT,
            json={"content": reassembled_html},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"LlamaBot {api_token}",
            },
            timeout=30,  # 30 second timeout
        )

    if response.status_code == 200:
        data = response.json()
        return json.dumps(data, ensure_ascii=False, indent=2)
    else:
        return f"HTTP Error {response.status_code}: {response.text}"


# Global tools list
tools = [write_html_page, get_weather, handle_selected_element]


# Node
def llamapress(state: LlamaPressState):
    additional_instructions = state.get("agent_prompt")
    # System message
    sys_msg = SystemMessage(
        content=f"""You are Leonardo the Llama, a helpful AI assistant. You live within LlamaPress, a web application that allows you to write full HTML pages with Tailwind CSS to the filesystem.
                        Any HTML pages generated MUST include tailwind CDN and viewport meta helper tags in the header: <EXAMPLE> <head data-llama-editable="true" data-llama-id="0">
                        <meta content="width=device-width, initial-scale=1.0" name="viewport">
                        <script src="https://cdn.tailwindcss.com"></script> </EXAMPLE>
                        
                        Here are additional instructions provided by the user: <ADDITIONAL_STATE_AND_CONTEXT> {state} </ADDITIONAL_STATE_AND_CONTEXT> 

                        <USER_INSTRUCTIONS> {additional_instructions} </USER_INSTRUCTIONS>"""
    )

    llm = ChatOpenAI(model="o4-mini")

    # if state.get(
    #     "selected_element"
    # ) is not None:  # only bind tools that are relevant for this state. (this is one way to control the agent's attention & decision making mechanistically)
    #     tools_to_bind = [handle_selected_element]
    # else:
    #     tools_to_bind = [write_html_page, get_weather]

    llm_with_tools = llm.bind_tools(tools)
    llm_response_message = llm_with_tools.invoke([sys_msg] + state["messages"])
    llm_response_message.response_metadata["created_at"] = str(datetime.now())

    return {"messages": [llm_response_message]}


def build_workflow(checkpointer=None):
    # Graph
    builder = StateGraph(LlamaPressState)

    # Define nodes: these do the work
    builder.add_node("llamapress", llamapress)
    builder.add_node("tools", ToolNode(tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "llamapress")
    builder.add_conditional_edges(
        "llamapress",
        # If the latest message (result) from llamapress is a tool call -> tools_condition routes to tools
        # If the latest message (result) from llamapress is a not a tool call -> tools_condition routes to END
        tools_condition,
    )
    builder.add_edge("tools", "llamapress")

    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph