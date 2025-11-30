from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

from langchain_core.tools import tool, InjectedToolCallId
from dotenv import load_dotenv
from functools import partial
from typing import Optional, Literal
import os
import logging
import requests
import json
from typing import Annotated
from datetime import datetime
import httpx
from typing_extensions import TypedDict

from .helpers import reassemble_fragments

load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage

from langgraph.graph import START, END, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode, InjectedState
from langgraph.types import Command


logger = logging.getLogger(__name__)

# Todo type definition
class Todo(TypedDict):
    """A structured task item for tracking progress through complex workflows.

    Attributes:
        content: Short, specific description of the task
        status: Current state - pending, in_progress, or completed
    """
    content: str
    status: Literal["pending", "in_progress", "completed"]

# Warning: Brittle - None type will break this when it's injected into the state for the tool call, and it silently fails. So if it doesn't map state types properly from the frontend, it will break. (must be exactly what's defined here).
class LlamaPressState(MessagesState):
    api_token: str
    agent_prompt: str
    page_id: str
    current_page_html: str
    selected_element: Optional[str]
    javascript_console_errors: Optional[str]
    todos: Optional[list[Todo]]
    created_at: Optional[datetime] = datetime.now()

# Core Logic Functions

async def write_html_page_logic(
    full_html_document: str,
    page_id: str,
    api_token: str,
    message_to_user: str = "",
    internal_thoughts: str = ""
) -> str:
    """
    Core logic to write an HTML page to the filesystem via the API.
    """
    # Configuration
    LLAMAPRESS_API_URL = os.getenv("LLAMAPRESS_API_URL")

    logger.info(f"Writing HTML page to filesystem! to {LLAMAPRESS_API_URL}")

    API_ENDPOINT = f"{LLAMAPRESS_API_URL}/pages/{page_id}.json"
    try:
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
            # data = response.json() # unused
            return json.dumps({
                'tool_name': 'write_html_page',
                'tool_args': {
                    'full_html_document': full_html_document,
                    'message_to_user': message_to_user,
                    'internal_thoughts': internal_thoughts
                }
            })
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


async def view_current_page_logic(page_id: str, api_token: str) -> str:
    """
    Core logic to fetch the current HTML page content.
    Returns the HTML content or an error message.
    """
    LLAMAPRESS_API_URL = os.getenv("LLAMAPRESS_API_URL")
    API_ENDPOINT = f"{LLAMAPRESS_API_URL}/pages/{page_id}/preview"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                API_ENDPOINT,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"LlamaBot {api_token}",
                },
                timeout=30,
            )
        if response.status_code == 200:
            return response.text
        else:
            return f"HTTP Error {response.status_code}: {response.text}"

    except httpx.ConnectError:
        return "Error: Could not connect to server. Make sure your app is running."

    except httpx.TimeoutException:
        return "Error: Request timed out."

    except Exception as e:
        return f"Unexpected Error: {str(e)}"


async def edit_file_logic(
    old_string: str,
    new_string: str,
    current_html: str,
    page_id: str,
    api_token: str
) -> str:
    """
    Core logic to edit the current HTML page by replacing old_string with new_string.
    """
    if not current_html:
        return "Error: No current page HTML available to edit"

    # Check if old_string exists in current HTML
    if old_string not in current_html:
        return (
            f"Error: Could not find the old_string in the current page HTML.\n\n"
            f"Suggestions:\n"
            f"1. The string you're looking for might not exist exactly as specified\n"
            f"2. Check for whitespace differences (spaces, tabs, newlines)\n"
            f"3. Try providing a smaller, more specific substring\n"
            f"4. Consider using write_html_page to write the entire document instead"
        )

    # Check for multiple occurrences
    occurrences = current_html.count(old_string)
    if occurrences > 1:
        return (
            f"Error: String appears {occurrences} times in the page. "
            f"Please provide a more specific string with surrounding context to ensure the correct instance is replaced."
        )

    # Perform the replacement
    new_html = current_html.replace(old_string, new_string, 1)

    # Now write the updated HTML to the LlamaPress API
    LLAMAPRESS_API_URL = os.getenv("LLAMAPRESS_API_URL")
    API_ENDPOINT = f"{LLAMAPRESS_API_URL}/pages/{page_id}.json"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.put(
                API_ENDPOINT,
                json={"content": new_html},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"LlamaBot {api_token}",
                },
                timeout=30,
            )

        if response.status_code == 200:
            return "Successfully edited the page (replaced 1 instance)"
        else:
            return f"HTTP Error {response.status_code}: {response.text}"

    except httpx.ConnectError:
        return "Error: Could not connect to server. Make sure your app is running."

    except httpx.TimeoutException:
        return "Error: Request timed out."

    except Exception as e:
        return f"Unexpected Error: {str(e)}"


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
    logger.info(f"API TOKEN: {state.get('api_token')}")
    logger.info(f"Page ID: {state.get('page_id')}")
    logger.info(
        f"State keys: {list(state.keys()) if isinstance(state, dict) else 'Not a dict'}"
    )

    # Get page_id from state, with fallback
    page_id = state.get("page_id")
    if not page_id:
        return "Error: page_id is required but not provided in state"

    # Get API token from state
    api_token = state.get("api_token")
    if not api_token:
        return "Error: api_token is required but not provided in state"

    return await write_html_page_logic(
        full_html_document,
        page_id,
        api_token,
        message_to_user,
        internal_thoughts
    )


@tool
async def view_current_page(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """
    Fetch and view the current HTML page content from the server.
    Use this tool when you need to see the latest version of the page before making edits.
    This ensures you're working with up-to-date content.
    """
    page_id = state.get("page_id")
    api_token = state.get("api_token")

    if not page_id:
        error_message = "Error: page_id is required but not provided in state"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )

    if not api_token:
        error_message = "Error: api_token is required but not provided in state"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )

    result = await view_current_page_logic(page_id, api_token)

    if result.startswith("Error") or result.startswith("HTTP Error") or result.startswith("Unexpected Error"):
        return Command(
            update={
                "messages": [ToolMessage(result, tool_call_id=tool_call_id)]
            }
        )
    else:
        current_html = result
        success_message = f"<CURRENT_PAGE_HTML>\n{current_html}\n</CURRENT_PAGE_HTML>"
        return Command(
            update={
                "messages": [ToolMessage(success_message, tool_call_id=tool_call_id)],
                "current_page_html": current_html  # Update state with latest HTML
            }
        )


@tool
def write_todos(
    todos: list[Todo],
    tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """
    Write a list of todos to track progress through complex workflows.
    Each todo should have a content (description) and status (pending, in_progress, or completed).
    """
    return Command(
        update={
            "todos": todos,
            "messages": [
                ToolMessage(f"Updated todo list to {len(todos)} items", tool_call_id=tool_call_id)
            ],
        }
    )


@tool
async def edit_file(
    old_string: str,
    new_string: str,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """
    Edit the current HTML page by replacing old_string with new_string.
    This is useful for making targeted edits to specific parts of the page without rewriting the entire document.

    Args:
        old_string: The exact string to find and replace in the current page
        new_string: The new string to replace it with
        state: The current state (injected automatically)
        tool_call_id: The tool call ID (injected automatically)

    Returns:
        Command with success or error message
    """
    # Get current page HTML
    current_html = state.get("current_page_html", "")
    page_id = state.get("page_id")
    api_token = state.get("api_token")

    if not page_id:
        error_message = "Error: page_id is required but not provided in state"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )

    if not api_token:
        error_message = "Error: api_token is required but not provided in state"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )

    result = await edit_file_logic(old_string, new_string, current_html, page_id, api_token)

    if result.startswith("Error") or result.startswith("HTTP Error") or result.startswith("Unexpected Error"):
        return Command(
            update={
                "messages": [ToolMessage(result, tool_call_id=tool_call_id)]
            }
        )
    else:
        return Command(
            update={
                "messages": [ToolMessage(result, tool_call_id=tool_call_id)]
            }
        )


# Node
def write_html_page_agent(state: LlamaPressState):
    # Build context-aware system prompt
    selected_element = state.get("selected_element")

    if selected_element:
        # User has selected a specific element
        system_content = (
            f"You are given an HTML and Tailwind snippet of code to inspect. Here it is: {selected_element}\n"
            "The user needs you to respond to their message. You have several tools available:\n"
            "- `view_current_page`: Fetch the latest HTML from the server (use this FIRST to see up-to-date content)\n"
            "- `edit_file`: Make targeted edits by replacing specific strings in the current page\n"
            "- `write_html_page`: Write the entire HTML document (use for major changes)\n"
            "- `write_todos`: Track progress with todo lists\n\n"
            "IMPORTANT: Before making edits, use `view_current_page` to see the latest version of the page.\n"
            "When modifying the selected element, make sure to preserve the context and structure while applying the requested changes.\n"
            "IF you are using the `write_html_page` function/tool, and are generating HTML/CSS/JavaScript code, include comments that explain what you're doing for each logical block of code you're about to generate.\n"
            "For every logical block you generate (HTML section, CSS rule set, JS function):\n"
            "1. Precede it with exactly **one** comment line that starts with 'CODE_EXPLANATION: <code_explanation> writing a section that ... </code_explanation>'\n"
            "2. Keep the code_explanation ≤ 15 words.\n"
            "3. Never include other text on that line.\n"
            "4. Examples of how to do this:\n"
            "   EXAMPLE_HTML_COMMENT <!-- <code_explanation>Adding a section about the weather</code_explanation> -->\n"
            "   EXAMPLE_TAILWIND_CSS_COMMENT /* <code_explanation>Setting the page background color to blue with Tailwind CSS</code_explanation> */\n"
            "   EXAMPLE_JAVASCRIPT_COMMENT // <code_explanation>Making the weather section interactive and animated with JavaScript</code_explanation>\n"
            "You DONT HAVE to write HTML code, and in fact sometimes it is inappropriate depending on what the user is asking you to do.\n"
            "You can also just respond and answer questions, or even ask clarifying questions, etc. Parse the user's intent and make a decision."
        )
    else:
        # Full page editing mode
        system_content = (
            "You are currently viewing an HTML Page and Tailwind CSS full page.\n"
            "The user needs you to respond to their message. You have several tools available:\n"
            "- `view_current_page`: Fetch the latest HTML from the server (use this FIRST to see up-to-date content)\n"
            "- `edit_file`: Make targeted edits by replacing specific strings in the current page\n"
            "- `write_html_page`: Write the entire HTML document (use for major changes)\n"
            "- `write_todos`: Track progress with todo lists\n\n"
            "IMPORTANT: Before making edits, use `view_current_page` to see the latest version of the page.\n"
            "IF you are using the `write_html_page` function/tool, and are generating HTML/CSS/JavaScript code, include comments that explain what you're doing for each logical block of code you're about to generate.\n"
            "For every logical block you generate (HTML section, CSS rule set, JS function):\n"
            "1. Precede it with exactly **one** comment line that starts with 'CODE_EXPLANATION: <code_explanation> writing a section that ... </code_explanation>'\n"
            "2. Keep the code_explanation ≤ 15 words.\n"
            "3. Never include other text on that line.\n"
            "4. Examples of how to do this:\n"
            "   EXAMPLE_HTML_COMMENT <!-- <code_explanation>Adding a section about the weather</code_explanation> -->\n"
            "   EXAMPLE_TAILWIND_CSS_COMMENT /* <code_explanation>Setting the page background color to blue with Tailwind CSS</code_explanation> */\n"
            "   EXAMPLE_JAVASCRIPT_COMMENT // <code_explanation>Making the weather section interactive and animated with JavaScript</code_explanation>\n"
            "5. You are able to write the new HTML and Tailwind snippet of code to the filesystem, if the user asks you to.\n"
            "   Any HTML pages generated MUST include tailwind CDN and viewport meta helper tags in the header:\n"
            "   <EXAMPLE> <head data-llama-editable='true' data-llama-id='0'>\n"
            "   <meta content='width=device-width, initial-scale=1.0' name='viewport'>\n"
            "   <script src='https://cdn.tailwindcss.com'></script> </EXAMPLE>\n"
            "You DONT HAVE to write HTML code, and in fact sometimes it is inappropriate depending on what the user is asking you to do.\n"
            "You can also just respond and answer questions, or even ask clarifying questions, etc. Parse the user's intent and make a decision."
        )

    model = ChatAnthropic(
            model="claude-haiku-4-5",
            max_tokens=16384
    )

    llm_with_tools = model.bind_tools(tools)
    llm_response_message = llm_with_tools.invoke([SystemMessage(content=system_content)] + state["messages"])
    llm_response_message.response_metadata["created_at"] = str(datetime.now())

    return {"messages": [llm_response_message]}

# Global tools list
tools = [write_html_page, write_todos, edit_file, view_current_page]

def build_workflow(checkpointer=None):
    # Graph
    builder = StateGraph(LlamaPressState)

    # Define nodes: these do the work
    builder.add_node("write_html_page_agent", write_html_page_agent)
    builder.add_node("tools", ToolNode(tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "write_html_page_agent")

    builder.add_conditional_edges(
        "write_html_page_agent",
        # If the latest message (result) from agent is a tool call -> tools_condition routes to tools
        # If the latest message (result) from agent is not a tool call -> tools_condition routes to END
        tools_condition,
    )

    builder.add_edge("tools", "write_html_page_agent")

    html_agent = builder.compile(checkpointer=checkpointer, name="html_agent")

    return html_agent