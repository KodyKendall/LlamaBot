from langchain_core.tools import tool, InjectedToolCallId
from langgraph.types import Command
from langchain_core.messages import ToolMessage
from typing import Annotated
from langgraph.prebuilt import InjectedState
from tavily import TavilyClient
import os
from bs4 import BeautifulSoup

from app.agents.rails_agent.prompts import (
    WRITE_TODOS_DESCRIPTION,
    EDIT_DESCRIPTION,
    TOOL_DESCRIPTION,
    INTERNET_SEARCH_DESCRIPTION,
    LIST_DIRECTORY_DESCRIPTION,
)

from app.agents.rails_agent.state import Todo, RailsAgentState

from pathlib import Path
import subprocess
import json
import re

from jinja2 import Environment, FileSystemLoader


# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Single HTML output file
HTML_OUTPUT_PATH = APP_DIR / 'page.html'

@tool(description="Write the requirements in HTML format (making it easy for user to view), for the user to see based on the current state of the project.")
def write_todo_message_to_user(message_to_user: str) -> str:
    """
    This is used for writing a nice, readable, and up to date requirements.txt file for the user to see based on the current state of the project.
    This will be with Tailwind CSS, and will be displayed in an iframe in the user's browser, starting with <!DOCTYPE html> <html> and ending with </html>.
    Make it look nice, and use the current state of the project to update the readme.
    """

    env = Environment(loader=FileSystemLoader(APP_DIR / 'agents' / 'rails_agent' / 'templates'))
    template = env.get_template("todo.html")

    html = template.render(message=message_to_user)

    with open(HTML_OUTPUT_PATH, "w", encoding='utf-8') as f:
        f.write(html)
    return f"HTML code written to {HTML_OUTPUT_PATH.relative_to(PROJECT_ROOT)}"

@tool(description="Read the requirements file and return the contents")
def read_requirements_txt() -> str:
    """
    Read the requirements.txt file and return the contents.
    """
    with open(HTML_OUTPUT_PATH, "r", encoding='utf-8') as f:
        raw_html_content = f.read()
        parsed_html_content = BeautifulSoup(raw_html_content, 'html.parser')
        return parsed_html_content.get_text()

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

@tool(description=LIST_DIRECTORY_DESCRIPTION)
def ls(directory: str = "") -> list[str]:
    """
    List the contents of a directory.
    If directory string is empty, lists the root directory.
    """
    # Build path - if directory is empty, just use rails root
    dir_path = APP_DIR / "rails" / directory if directory else APP_DIR / "rails"
    
    if not dir_path.exists():
        return f"Directory not found: {directory}"
    
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

    return Command(
        update={
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

    return Command(
        update={
            "messages": [ToolMessage(result_msg, tool_call_id=tool_call_id)],
        }
    )

@tool(description="Search all files in the project directory for a substring")
def search_file(
    substring: str, tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    """Search all files in the directory for a substring."""
    full_path = APP_DIR / "rails"
    matches = []
    
    # Check if the rails directory exists
    if not full_path.exists():
        result_msg = f"Project directory not found"
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
        result_msg = f"Substring '{substring}' not found in any files in the directory"
    
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

# Rails container configuration
RAILS_CONT = "rails-agent-llamapress-1"
WORKDIR = "/rails"  # path that contains bin/rails inside the Rails container

def rails_api_sh(snippet: str, workdir: str = WORKDIR) -> str:
    """Execute a command in the Rails Docker container via Docker API."""
    try:
        # Create the exec payload
        payload = {
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": True,
            "Cmd": ["/bin/sh", "-lc", snippet],
            "WorkingDir": workdir
        }
        
        # Create exec instance using curl
        create_cmd = [
            "curl", "--silent", "--show-error", "--fail-with-body",
            "--unix-socket", "/var/run/docker.sock",
            "-H", "Content-Type: application/json",
            "--data-binary", json.dumps(payload),
            f"http://localhost/containers/{RAILS_CONT}/exec"
        ]
        
        create_result = subprocess.run(create_cmd, capture_output=True, text=True, timeout=30)
        if create_result.returncode != 0:
            return f"CREATE-EXEC ERROR: {create_result.stderr or create_result.stdout}"
        
        # Parse exec ID
        try:
            exec_data = json.loads(create_result.stdout)
            exec_id = exec_data["Id"]
        except (KeyError, json.JSONDecodeError) as e:
            return f"BAD CREATE RESPONSE: {create_result.stdout}"
        
        # Validate exec ID format (64 character hex string)
        if not re.match(r'^[0-9a-f]{64}$', exec_id):
            return f"No exec Id parsed; aborting. Got: {exec_id}"
        
        # Start exec instance using curl
        start_cmd = [
            "curl", "-N", "--silent", "--show-error", "--fail-with-body",
            "--unix-socket", "/var/run/docker.sock",
            "-H", "Content-Type: application/json",
            "-d", '{"Detach":false,"Tty":true}',
            f"http://localhost/exec/{exec_id}/start"
        ]
        
        start_result = subprocess.run(start_cmd, capture_output=True, text=True, timeout=60)
        if start_result.returncode != 0:
            return f"START-EXEC ERROR: {start_result.stderr or start_result.stdout}"
        
        return start_result.stdout
        
    except subprocess.TimeoutExpired:
        return "Command timed out"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

@tool(description="Execute a bash command in operating system that Rails is running in")
def bash_command(
    command: str, 
    tool_call_id: Annotated[str, InjectedToolCallId],
    workdir: str = WORKDIR
) -> Command:
    """Execute a bash command in the Rails Docker container."""
    result = rails_api_sh(command, workdir)
    
    return Command(
        update={
            "messages": [
                ToolMessage(f"Command output:\n{result}", tool_call_id=tool_call_id)
            ],
        }
    )

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