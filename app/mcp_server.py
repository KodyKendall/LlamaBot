import logging
from mcp.server.fastmcp import FastMCP
from app.agents.llamapress.html_agent import write_html_page_logic, view_current_page_logic, edit_file_logic

# Configure logging
logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP("LlamaBot MCP Server")

@mcp.tool()
async def write_html_page(
    full_html_document: str,
    page_id: str,
    api_token: str,
    message_to_user: str = "",
    internal_thoughts: str = ""
) -> str:
    """
    Write an HTML page to the filesystem.
    full_html_document is the full HTML document to write to the filesystem, including CSS and JavaScript.
    message_to_user is a string to tell the user what you're doing.
    internal_thoughts are your thoughts about the command.
    """
    return await write_html_page_logic(
        full_html_document,
        page_id,
        api_token,
        message_to_user,
        internal_thoughts
    )

@mcp.tool()
async def view_current_page(page_id: str, api_token: str) -> str:
    """
    Fetch and view the current HTML page content from the server.
    Use this tool when you need to see the latest version of the page before making edits.
    This ensures you're working with up-to-date content.
    """
    return await view_current_page_logic(page_id, api_token)

@mcp.tool()
async def edit_file(
    old_string: str,
    new_string: str,
    current_html: str,
    page_id: str,
    api_token: str
) -> str:
    """
    Edit the current HTML page by replacing old_string with new_string.
    This is useful for making targeted edits to specific parts of the page without rewriting the entire document.
    """
    return await edit_file_logic(
        old_string,
        new_string,
        current_html,
        page_id,
        api_token
    )
