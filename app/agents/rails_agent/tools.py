from langchain_core.tools import tool, InjectedToolCallId
from langgraph.types import Command
from langchain_core.messages import ToolMessage
from typing import Annotated
from langgraph.prebuilt import InjectedState
from tavily import TavilyClient
import os

from app.agents.rails_agent.prompts import (
    WRITE_TODOS_DESCRIPTION,
    EDIT_DESCRIPTION,
    TOOL_DESCRIPTION,
    INTERNET_SEARCH_DESCRIPTION,
)

from app.agents.rails_agent.state import Todo, RailsAgentState

from pathlib import Path

# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

@tool(description=WRITE_TODOS_DESCRIPTION)
def write_todos(
    todos: list[Todo], tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    return Command(
        update={
            "todos": todos,
            "messages": [
                ToolMessage(f"Updated todo list to {todos}", tool_call_id=tool_call_id)
            ],
        }
    )

@tool
def ls(directory: str = "") -> list[str]:
    """
    List the contents of a directory.
    If directory is empty, lists the rails root directory.
    """
    # Build path - if directory is empty, just use rails root
    dir_path = APP_DIR / "rails" / directory if directory else APP_DIR / "rails"
    
    if not dir_path.exists():
        return f"Directory not found: {dir_path}"
    
    return os.listdir(dir_path)

@tool(description=TOOL_DESCRIPTION)
def read_file(
    file_path: str,
    state: Annotated[RailsAgentState, InjectedState],
    offset: int = 0,
    limit: int = 2000,
) -> str:
    """Read file."""
    # Construct the full path
    full_path = APP_DIR / "rails" / file_path
    
    # Check if file exists
    if not full_path.exists():
        return f"Error: File '{file_path}' not found"
    
    # Read the file contents
    try:
        content = full_path.read_text()
    except Exception as e:
        return f"Error reading file: {e}"

    # Handle empty file
    if not content or content.strip() == "":
        return "System reminder: File exists but has empty contents"

    # Split content into lines
    lines = content.splitlines()

    # Apply line offset and limit
    start_idx = offset
    end_idx = min(start_idx + limit, len(lines))

    # Handle case where offset is beyond file length
    if start_idx >= len(lines):
        return f"Error: Line offset {offset} exceeds file length ({len(lines)} lines)"

    # Format output with line numbers (cat -n format)
    result_lines = []
    for i in range(start_idx, end_idx):
        line_content = lines[i]

        # Truncate lines longer than 2000 characters
        if len(line_content) > 2000:
            line_content = line_content[:2000]

        # Line numbers start at 1, so add 1 to the index
        line_number = i + 1
        result_lines.append(f"{line_number:6d}\t{line_content}")

    return "\n".join(result_lines)


@tool(description="Write to a file.")
def write_file(
    file_path: str,
    content: str,
    state: Annotated[RailsAgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Write to a file."""
    full_path = APP_DIR / "rails" / file_path
    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
    except Exception as e:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error writing file {file_path}: {e}",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    files = state.get("files", {})
    files[file_path] = content
    return Command(
        update={
            "files": files,
            "messages": [
                ToolMessage(f"Updated file {file_path}", tool_call_id=tool_call_id)
            ],
        }
    )


@tool(description=EDIT_DESCRIPTION)
def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    state: Annotated[RailsAgentState, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    replace_all: bool = False,
) -> Command:
    """Perform string replacement in a file."""
    full_path = APP_DIR / "rails" / file_path

    if not full_path.exists():
        error_message = f"Error: File '{file_path}' not found"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )
    try:
        content = full_path.read_text()
    except Exception as e:
        error_message = f"Error reading file '{file_path}': {e}"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )

    if old_string not in content:
        error_message = f"Error: String not found in file: '{old_string}'"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )

    if not replace_all:
        occurrences = content.count(old_string)
        if occurrences > 1:
            error_message = f"Error: String '{old_string}' appears {occurrences} times in file. Use replace_all=True to replace all instances, or provide a more specific string with surrounding context."
            return Command(
                update={
                    "messages": [
                        ToolMessage(error_message, tool_call_id=tool_call_id)
                    ]
                }
            )

    if replace_all:
        new_content = content.replace(old_string, new_string)
        replacement_count = content.count(old_string)
        result_msg = f"Successfully replaced {replacement_count} instance(s) of the string in '{file_path}'"
    else:
        new_content = content.replace(old_string, new_string, 1)
        result_msg = f"Successfully replaced string in '{file_path}'"

    try:
        full_path.write_text(new_content)
    except Exception as e:
        error_message = f"Error writing to file '{file_path}': {e}"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )

    files = state.get("files", {})
    files[file_path] = new_content
    return Command(
        update={
            "files": files,
            "messages": [ToolMessage(result_msg, tool_call_id=tool_call_id)],
        }
    )

@tool(description="Search all files in the rails directory for a substring")
def search_file(
    substring: str, tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """Search all files in the rails directory for a substring."""
    full_path = APP_DIR / "rails"
    matches = []
    
    # Check if the rails directory exists
    if not full_path.exists():
        result_msg = f"Rails directory not found: {full_path}"
        return Command(
            update={
                "messages": [ToolMessage(result_msg, tool_call_id=tool_call_id)],
            }
        )
    
    # Recursively iterate through all files in subdirectories
    for file_path in full_path.rglob("*"):
        if file_path.is_file():  # Only check actual files, not directories
            try:
                content = file_path.read_text()
                if substring in content:
                    # Get relative path from rails directory for cleaner output
                    relative_path = file_path.relative_to(full_path)
                    matches.append(str(relative_path))
            except (UnicodeDecodeError, PermissionError, OSError):
                # Skip files that can't be read (binary files, permission issues, etc.)
                continue

    if matches:
        if len(matches) == 1:
            result_msg = f"Substring '{substring}' found in 1 file:\n- {matches[0]}"
        else:
            result_msg = f"Substring '{substring}' found in {len(matches)} files:\n" + "\n".join(f"- {match}" for match in matches)
    else:
        result_msg = f"Substring '{substring}' not found in any files in the rails directory"
    
    return Command(
        update={
            "messages": [ToolMessage(result_msg, tool_call_id=tool_call_id)],
        }
    )


def list_all_files_recursive(directory: Path):
    """
    Example function showing different ways to recursively iterate through all files
    """
    print(f"Files in {directory} and all subdirectories:")

    # Method 1: Using pathlib.Path.rglob() (recommended for most cases)
    print("\n1. Using pathlib.rglob():")
    for file_path in directory.rglob("*"):
        if file_path.is_file():
            print(f"  {file_path.relative_to(directory)}")

    # Method 2: Using os.walk()
    print("\n2. Using os.walk():")
    for root, dirs, files in os.walk(directory):
        for file in files:
            file_path = Path(root) / file
            print(f"  {file_path.relative_to(directory)}")

    # Method 3: Using glob.glob() with recursive pattern
    print("\n3. Using glob.glob() with recursive pattern:")
    import glob
    for file_path in glob.glob(str(directory / "**" / "*"), recursive=True):
        file_path = Path(file_path)
        if file_path.is_file():
            print(f"  {file_path.relative_to(directory)}")

    # Method 4: Using pathlib with custom recursion
    print("\n4. Using custom recursion:")
    def walk_directory(path: Path, prefix=""):
        for item in path.iterdir():
            if item.is_file():
                print(f"  {prefix}{item.name}")
            elif item.is_dir():
                print(f"  {prefix}{item.name}/")
                walk_directory(item, prefix + "  ")

    walk_directory(directory)


tavily_client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
@tool(description=INTERNET_SEARCH_DESCRIPTION)
# Search tool to use to do research
def internet_search(
    query: str,
    max_results: int = 5,
    include_raw_content: bool = False,
):
    search_docs = tavily_client.search(
        query,
        max_results=max_results,
        include_raw_content=include_raw_content,
        topic="general",
    )
    return search_docs