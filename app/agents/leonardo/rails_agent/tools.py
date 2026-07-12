from langchain.tools import tool, ToolRuntime
from langgraph.types import Command
from langchain_core.messages import ToolMessage
from tavily import TavilyClient
from typing import Optional
import os
import time
import base64
from bs4 import BeautifulSoup

# Import checkpoint service for auto-checkpointing before file edits
from app.services.checkpoint_service import checkpoint_service

from app.agents.leonardo.rails_agent.prompts import (
    WRITE_TODOS_DESCRIPTION,
    EDIT_DESCRIPTION,
    TOOL_DESCRIPTION,
    LIST_DIRECTORY_DESCRIPTION,
    BASH_COMMAND_FOR_RAILS_DESCRIPTION,
    GLOB_FILES_DESCRIPTION,
    GREP_FILES_DESCRIPTION,
)

from app.agents.leonardo.rails_agent.tool_prompts import (
    INTERNET_SEARCH_DESCRIPTION,
    GIT_STATUS_DESCRIPTION,
    GIT_COMMIT_DESCRIPTION,
    GIT_COMMAND_DESCRIPTION,
    GITHUB_CLI_DESCRIPTION,
    SAVE_MEMORY_DESCRIPTION,
    LIST_MEMORIES_DESCRIPTION,
    DELETE_MEMORY_DESCRIPTION,
    READ_LEONARDO_MD_DESCRIPTION,
    EDIT_LEONARDO_MD_DESCRIPTION,
    WRITE_LEONARDO_MD_DESCRIPTION,
    TAIL_RAILS_LOGS_DESCRIPTION,
    HARD_RESTART_RAILS_DESCRIPTION,
    FIX_PERMISSIONS_DESCRIPTION,
    BROWSER_INSPECT_DESCRIPTION,
    USE_SKILL_DESCRIPTION_TEMPLATE,
    LIST_SKILLS_DESCRIPTION,
    READ_SKILL_DESCRIPTION,
    WRITE_SKILL_DESCRIPTION,
    EDIT_SKILL_DESCRIPTION,
    DELETE_SKILL_DESCRIPTION,
    READ_BRAND_GUIDE_DESCRIPTION,
    WRITE_BRAND_GUIDE_DESCRIPTION,
)

from app.agents.leonardo.project_context import (
    LEONARDO_MD_PATH,
    SOUL_MD_PATH,
    USER_MD_PATH,
    IDENTITY_MD_PATH,
)

from app.services.brand_service import load_brand, save_brand, render_brand_md

from app.agents.leonardo.memory import (
    write_memory_file,
    list_all_memories,
    delete_memory_file,
)

from app.agents.leonardo.skills import (
    list_all_skills,
    get_skill_body,
    write_skill_file,
    edit_skill_file,
    delete_skill_file,
    render_available_skills,
)

from app.agents.leonardo.rails_agent.state import Todo

from pathlib import Path
import subprocess
import json
import re
import difflib

from jinja2 import Environment, FileSystemLoader


# ============================================================================
# Bash Output Configuration
# ============================================================================

# Maximum characters for bash command output before truncation
BASH_OUTPUT_MAX_CHARS = 12000

# Moderate error detection - permission errors + common Rails errors
# (excludes test failure patterns like "FAILED" to avoid false positives on intentional test runs)
BASH_ERROR_PATTERNS = [
    # Permission errors (critical)
    "Permission denied",
    "EACCES",
    "Operation not permitted",
    "Read-only file system",
    # File system errors
    "No such file or directory",
    # Ruby/Rails errors
    "LoadError",
    "SyntaxError",
    "NameError",
    "NoMethodError",
    "ArgumentError",
    # Database errors
    "ActiveRecord::StatementInvalid",
    "PG::Error",
    "Mysql2::Error",
    # Process errors
    "command not found",
    "Killed",
    "Segmentation fault",
]

# Critical errors that indicate infrastructure issues that CANNOT be fixed from inside container
BASH_CRITICAL_ERROR_PATTERNS = [
    "Permission denied",
    "EACCES",
    "Operation not permitted",
    "Read-only file system",
]


def detect_bash_errors(output: str) -> tuple[bool, bool, list[str]]:
    """Detect semantic errors in bash command output.

    Returns:
        tuple of (has_error, is_critical, matched_patterns)
        - has_error: True if any error pattern was found
        - is_critical: True if a critical/unrecoverable error was found
        - matched_patterns: List of matched error pattern strings
    """
    matched = [p for p in BASH_ERROR_PATTERNS if p in output]
    is_critical = any(p in output for p in BASH_CRITICAL_ERROR_PATTERNS)
    return (len(matched) > 0, is_critical, matched)


def truncate_output(output: str, max_chars: int = BASH_OUTPUT_MAX_CHARS) -> str:
    """Truncate output if it exceeds max_chars, preserving beginning and end.

    Strategy: Keep first 50% and last 50% of allowed characters to preserve
    both the command start context and the final output/errors/summaries.
    """
    if len(output) <= max_chars:
        return output

    head_chars = int(max_chars * 0.5)
    tail_chars = int(max_chars * 0.5)

    truncation_msg = f"\n\n[...OUTPUT TRUNCATED - {len(output) - max_chars} characters removed...]\n\n"

    return output[:head_chars] + truncation_msg + output[-tail_chars:]


# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

# Ubuntu user compatibility: chown files to UID 1000 after writing
# LlamaBot container runs as root, but host ubuntu user is UID 1000
# Without this, the ubuntu user can't edit files created by the agent
UBUNTU_UID = 1000
UBUNTU_GID = 1000


def chown_for_ubuntu(path: Path) -> None:
    """Change file ownership to UID 1000:1000 for ubuntu user compatibility.

    The LlamaBot container runs as root, but the host ubuntu user is UID 1000.
    This ensures the ubuntu user can edit files created by the agent.
    Silently ignores errors (e.g., if chown not available or fails).
    """
    try:
        os.chown(path, UBUNTU_UID, UBUNTU_GID)
    except (OSError, PermissionError):
        # Silently ignore - this is a best-effort operation
        pass

@tool(description=WRITE_TODOS_DESCRIPTION)
def write_todos(
    todos: list[Todo],
    runtime: ToolRuntime,
) -> Command:
    """Update the todo list with new items."""
    return Command(
        update={
            "todos": todos,
            "messages": [
                ToolMessage(f"Updated todo list to {todos}", tool_call_id=runtime.tool_call_id)
            ],
        }
    )

def guard_against_beginning_slash_argument(argument: str) -> str:
    """
    Normalize file paths that LLMs might format incorrectly.
    Handles cases like:
    - /rails/app/views -> app/views
    - rails/app/views -> app/views
    - app/app/views -> app/views
    - /app/views -> app/views
    """
    # Strip leading slashes
    if argument.startswith("/"):
        argument = argument[1:]

    # Strip 'rails/' prefix if present
    if argument.startswith("rails/"):
        argument = argument[6:]  # len("rails/") = 6

    # Reduce 'app/app/' to just 'app/'
    if argument.startswith("app/app/"):
        argument = argument[4:]  # Remove the first "app/"

    return argument

def normalize_whitespace(s: str) -> str:
    """Normalize whitespace for more flexible string matching.

    This helps handle differences in:
    - Line endings (CRLF vs LF)
    - Spaces vs tabs
    - Multiple consecutive spaces/newlines
    """
    # Normalize line endings
    s = s.replace('\r\n', '\n')
    # Collapse multiple spaces/tabs to single space (but preserve indentation structure)
    s = re.sub(r'[ \t]+', ' ', s)
    # Collapse multiple newlines to single newline
    s = re.sub(r'\n\n+', '\n\n', s)
    return s.strip()


# NOTE: Auto-checkpoint functionality has been disabled.
# Users now manually create checkpoints via the History panel UI.
# The checkpoint_service is still used by the /api/checkpoints endpoint for manual creation.
#
# def maybe_create_checkpoint(runtime: ToolRuntime, description: str):
#     """Create a git checkpoint before file modifications if not already created this turn.
#     ...
#     """
#     pass


@tool(description=LIST_DIRECTORY_DESCRIPTION)
def ls(directory: str = "") -> list[str]:
    if directory.startswith("/"): # we NEVER want to include a leading slash "/"  at the beginning of the directory string. It's all relative in our docker container.
        directory = directory[1:]

    # Build path - if directory is empty, just use rails root
    dir_path = APP_DIR / "rails" / directory if directory else APP_DIR / "rails"
    
    if not dir_path.exists():
        return f"Directory not found: {directory}"
    
    return os.listdir(dir_path)

@tool(description=TOOL_DESCRIPTION)
def read_file(
    file_path: str,
    runtime: ToolRuntime,
    offset: int = 0,
    limit: int = 2000,
) -> str:
    """Read a file within the Rails project and return its contents."""
    file_path = guard_against_beginning_slash_argument(file_path)
    
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


@tool(description="""This creates and writes to a file at the specicied path, creating the file and any necessary directories if they don't exist.
    Usage:
    - file_path: The path to the file to write to. This should be a relative path from the root of the Rails project. Never include a leading slash "/" at the beginning of the file_path.
    - content: The content to write to the file. You must specify this argument or this tool call will fail.""")
def write_file(
    file_path: str,
    content: str,
    runtime: ToolRuntime,
) -> Command:
    """Create or overwrite a file at the specified path."""
    file_path = guard_against_beginning_slash_argument(file_path)
    full_path = APP_DIR / "rails" / file_path

    # NOTE: Auto-checkpoint disabled. Users create checkpoints manually via History panel.

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
        chown_for_ubuntu(full_path)  # Fix permissions for ubuntu user
    except Exception as e:
        error_message = f"Error writing file {file_path}: {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        error_message,
                        artifact=tool_output,
                        tool_call_id=runtime.tool_call_id,
                    )
                ]
            }
        )

    success_message = f"Updated file {file_path}"
    tool_output = {
        "status": "success",
        "message": success_message
    }

    return Command(
        update={
            "messages": [
                ToolMessage(success_message, artifact=tool_output, tool_call_id=runtime.tool_call_id)
            ],
        }
    )


@tool(description=EDIT_DESCRIPTION)
def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    runtime: ToolRuntime,
    replace_all: bool = False,
) -> Command:
    """Edit a file by replacing old_string with new_string."""
    tool_call_id = runtime.tool_call_id
    file_path = guard_against_beginning_slash_argument(file_path)
    full_path = APP_DIR / "rails" / file_path

    # NOTE: Auto-checkpoint disabled. Users create checkpoints manually via History panel.

    if not full_path.exists():
        error_message = f"Error: File '{file_path}' not found"
        tool_output = {
            "status": "error",
            "message": error_message
        }

        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )
    try:
        content = full_path.read_text()
    except Exception as e:
        error_message = f"Error reading file '{file_path}': {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }

        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )

    # Try exact match first
    search_string = old_string
    match_found = old_string in content
    match_type = "exact"

    # If exact match fails, try normalized matching
    if not match_found:
        normalized_content = normalize_whitespace(content)
        normalized_old = normalize_whitespace(old_string)

        if normalized_old in normalized_content:
            # Find the actual substring in the original content that matches the normalized version
            # We'll use fuzzy matching to locate it
            match_found = True
            match_type = "normalized"
            search_string = normalized_old

            # Use difflib to find the best matching region
            matcher = difflib.SequenceMatcher(None, content, old_string)
            match = matcher.find_longest_match(0, len(content), 0, len(old_string))

            if match.size > len(old_string) * 0.7:  # At least 70% match
                # Extract the actual substring from content
                search_string = content[match.a:match.a + match.size]
                match_found = True
                match_type = "fuzzy"

    # If still no match, try fuzzy matching as last resort
    if not match_found:
        matcher = difflib.SequenceMatcher(None, content, old_string)
        similarity = matcher.ratio()

        if similarity > 0.6:  # 60% similarity threshold
            match = matcher.find_longest_match(0, len(content), 0, len(old_string))

            if match.size > len(old_string) * 0.5:  # At least 50% of the string
                search_string = content[match.a:match.a + match.size]
                match_found = True
                match_type = "fuzzy"

    # If still no match found, provide detailed error with diff
    if not match_found:
        # Generate a helpful diff preview
        content_lines = content.splitlines()
        old_string_lines = old_string.splitlines()

        # Limit diff preview to first 15 lines
        diff_lines = list(difflib.unified_diff(
            content_lines[:50],  # Show up to 50 lines of context
            old_string_lines[:50],
            fromfile='file_content',
            tofile='old_string_provided',
            lineterm=''
        ))[:20]  # Limit to 20 lines of diff

        diff_preview = '\n'.join(diff_lines) if diff_lines else "No meaningful diff available"

        error_message = (
            f"Error: Could not find old_string in file '{file_path}'.\n\n"
            f"<HINT>This content may come from dynamic rendering or ERB logic. "
            f"Use 'read_file' first to get the exact string from the source file, "
            f"not from rendered HTML.</HINT>\n\n"
            f"Diff preview (file vs your old_string):\n{diff_preview}\n\n"
            f"Suggestions:\n"
            f"1. Use read_file to verify the exact content\n"
            f"2. Provide a smaller, more specific substring\n"
            f"3. Check for whitespace differences (spaces, tabs, newlines)"
        )

        tool_output = {
            "status": "error",
            "message": error_message
        }


        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)],
                "failed_tool_calls_count": 1  # This will be added to the existing count due to operator.add reducer
            }
        )

    # Check for multiple occurrences
    if not replace_all:
        occurrences = content.count(search_string)
        if occurrences > 1:
            error_message = f"Error: String appears {occurrences} times in file. Use replace_all=True to replace all instances, or provide a more specific string with surrounding context."
            tool_output = {
                "status": "error",
                "message": error_message
            }

            return Command(
                update={
                    "messages": [
                        ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)],
                    "failed_tool_calls_count": 1  # This will be added to the existing count due to operator.add reducer
                }
            )

    # Perform the replacement
    if replace_all:
        new_content = content.replace(search_string, new_string)
        replacement_count = content.count(search_string)
        result_msg = f"Successfully replaced {replacement_count} instance(s) in '{file_path}' (match type: {match_type})"
    else:
        new_content = content.replace(search_string, new_string, 1)
        result_msg = f"Successfully replaced string in '{file_path}' (match type: {match_type})"

    try:
        full_path.write_text(new_content)
        chown_for_ubuntu(full_path)  # Fix permissions for ubuntu user
    except Exception as e:
        error_message = f"Error writing to file '{file_path}': {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }

        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)],
                "failed_tool_calls_count": 1  # This will be added to the existing count due to operator.add reducer
            }
        )

    # git_status(tool_call_id) # hacky - this will update the git status page so the user can see the changes.
    tool_output = {
        "status": "success",
        "message": result_msg
    }

    return Command(
        update={
            "messages": [ToolMessage(result_msg, artifact=tool_output, tool_call_id=tool_call_id)],
        }
    )



@tool(description=GLOB_FILES_DESCRIPTION)
def glob_files(
    pattern: str,
    runtime: ToolRuntime,
    path: str = "",
    max_results: int = 100,
) -> Command:
    """Find files matching a glob pattern using ripgrep."""
    tool_call_id = runtime.tool_call_id

    # Normalize path
    if path:
        path = guard_against_beginning_slash_argument(path)
        search_dir = APP_DIR / "rails" / path
    else:
        search_dir = APP_DIR / "rails"

    if not search_dir.exists():
        return Command(update={
            "messages": [ToolMessage(f"Directory not found: {path or 'rails root'}", tool_call_id=tool_call_id)]
        })

    # Use rg --files with glob pattern
    cmd = [
        "rg", "--files",
        "--glob", pattern,
        "--glob", "!.git",
        "--glob", "!node_modules",
        "--glob", "!tmp",
        "--glob", "!log",
        str(search_dir)
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and result.stdout:
            files = result.stdout.strip().split('\n')
            # Convert to relative paths
            rails_root = APP_DIR / "rails"
            files = [str(Path(f).relative_to(rails_root)) for f in files if f]
            total = len(files)
            files = files[:max_results]

            file_list = "\n".join(f"  {f}" for f in files)
            msg = f"Found {total} file(s) matching '{pattern}'"
            if total > max_results:
                msg += f" (showing first {max_results})"
            msg += f":\n{file_list}"
        else:
            msg = f"No files found matching pattern '{pattern}'"

    except subprocess.TimeoutExpired:
        msg = "Search timed out after 30 seconds"
    except FileNotFoundError:
        msg = "ripgrep (rg) not found. Please ensure it's installed in the container."
    except Exception as e:
        msg = f"Error during search: {e}"

    return Command(update={"messages": [ToolMessage(msg, tool_call_id=tool_call_id)]})


@tool(description=GREP_FILES_DESCRIPTION)
def grep_files(
    pattern: str,
    runtime: ToolRuntime,
    glob: str = "",
    path: str = "",
    case_insensitive: bool = False,
    context_lines: int = 0,
    max_results: int = 50,
) -> Command:
    """Search file contents for a regex pattern using ripgrep."""
    tool_call_id = runtime.tool_call_id

    # Normalize path
    if path:
        path = guard_against_beginning_slash_argument(path)
        search_dir = APP_DIR / "rails" / path
    else:
        search_dir = APP_DIR / "rails"

    if not search_dir.exists():
        return Command(update={
            "messages": [ToolMessage(f"Directory not found: {path or 'rails root'}", tool_call_id=tool_call_id)]
        })

    # Build ripgrep command
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never"]

    # Add options
    if case_insensitive:
        cmd.append("-i")
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    if glob:
        cmd.extend(["--glob", glob])

    # Always ignore common directories
    cmd.extend(["--glob", "!.git", "--glob", "!node_modules", "--glob", "!tmp", "--glob", "!log"])

    # Add pattern and path
    cmd.append(pattern)
    cmd.append(str(search_dir))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and result.stdout:
            lines = result.stdout.strip().split('\n')
            total = len([l for l in lines if l and not l.startswith('--')])  # Exclude context separators

            # Limit results
            if len(lines) > max_results * (1 + context_lines * 2):
                lines = lines[:max_results * (1 + context_lines * 2)]
                truncated = True
            else:
                truncated = False

            # Convert absolute paths to relative
            output_lines = []
            rails_root = str(APP_DIR / "rails") + "/"
            for line in lines:
                if line.startswith(rails_root):
                    line = line[len(rails_root):]
                output_lines.append(line)

            msg = f"Found matches for pattern '{pattern}':\n\n" + "\n".join(output_lines)
            if truncated:
                msg += f"\n\n(Results truncated. Use max_results parameter for more.)"
        elif result.returncode == 1:
            msg = f"No matches found for pattern '{pattern}'"
        else:
            msg = f"Search error: {result.stderr or 'Unknown error'}"

    except subprocess.TimeoutExpired:
        msg = "Search timed out after 30 seconds"
    except FileNotFoundError:
        msg = "ripgrep (rg) not found. Please ensure it's installed in the container."
    except Exception as e:
        msg = f"Error during search: {e}"

    return Command(update={"messages": [ToolMessage(msg, tool_call_id=tool_call_id)]})


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
WORKDIR = "/rails"  # path that contains bin/rails inside the Rails container

def get_rails_container_name():
    """Dynamically get the Rails container name by looking for containers with 'llamapress' in the name.

    This function is called on each rails_api_sh invocation to handle container restarts.
    Container names vary by environment:
    - Production: llamapress-1
    - Development: leonardo-llamapress-1 (or similar, based on docker-compose directory name)
    """
    try:
        # List all running containers using Docker API
        cmd = [
            "curl", "--silent", "--unix-socket", "/var/run/docker.sock",
            "http://localhost/containers/json"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

        if result.returncode == 0:
            containers = json.loads(result.stdout)
            for container in containers:
                # Container names are in the 'Names' array with leading '/'
                names = container.get('Names', [])
                for name in names:
                    # Remove leading '/' and check if it contains 'llamapress'
                    clean_name = name.lstrip('/')
                    # Match any container with 'llamapress' in the name (handles various prefixes)
                    if 'llamapress' in clean_name.lower() and 'llamabot' not in clean_name.lower():
                        return clean_name

        # Fallback to common names
        import os
        if os.environ.get('ENV') == 'production':
            return "llamapress-1"

        # Default to leonardo prefix (most common dev setup)
        return "leonardo-llamapress-1"

    except Exception as e:
        # If anything goes wrong, return a sensible default
        import logging
        logging.getLogger(__name__).warning(f"Failed to detect Rails container: {e}")
        return "leonardo-llamapress-1"


# Initialize container name at module load (used by capture_rails_logs)
RAILS_CONT = get_rails_container_name()

def rails_api_sh(snippet: str, workdir: str = WORKDIR, timeout_seconds: int = 60) -> str:
    """Execute a command in the Rails Docker container via Docker API.

    Args:
        snippet: The bash command to execute
        workdir: Working directory inside the container
        timeout_seconds: Maximum time to wait for command completion (default 60, max 600)
    """
    # Clamp timeout to reasonable bounds (30 seconds minimum to 10 minutes max)
    timeout_seconds = max(30, min(timeout_seconds, 600))

    try:
        # Get container name dynamically (handles restarts and varying prefixes)
        container_name = get_rails_container_name()

        # Create the exec payload
        payload = {
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": True,
            "Cmd": ["/bin/sh", "-lc", snippet],
            "WorkingDir": workdir,
            "User": "1000:1000",  # Run as UID 1000 to match host user and prevent permission issues
            "Env": ["RUBYOPT=-W0"]  # Suppress Ruby warnings (e.g., gem deprecation notices)
        }

        # Create exec instance using curl
        create_cmd = [
            "curl", "--silent", "--show-error", "--fail-with-body",
            "--unix-socket", "/var/run/docker.sock",
            "-H", "Content-Type: application/json",
            "--data-binary", json.dumps(payload),
            f"http://localhost/containers/{container_name}/exec"
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
        
        start_result = subprocess.run(start_cmd, capture_output=True, text=True, timeout=timeout_seconds)
        if start_result.returncode != 0:
            return f"START-EXEC ERROR: {start_result.stderr or start_result.stdout}"
        
        return start_result.stdout
        
    except subprocess.TimeoutExpired:
        return "Command timed out"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

def capture_rails_logs(duration: int = 10, output_file: str = None) -> str:
    """
    Capture Rails container logs for a duration and write to the rails folder.

    Args:
        duration: Seconds to capture logs (default 10)
        output_file: Path relative to rails folder (auto-generated if None)

    Returns:
        Path to the captured log file or error message
    """
    import random
    import string

    # Generate random 3-character suffix if no output file specified
    if output_file is None:
        suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=3))
        output_file = f"tmp/debug_rails_log_{suffix}.txt"

    time.sleep(duration)

    # Use docker logs API to capture stdout/stderr
    # Note: Docker logs API returns multiplexed stream with 8-byte headers per frame
    cmd = [
        "curl", "--silent", "--unix-socket", "/var/run/docker.sock",
        f"http://localhost/containers/{RAILS_CONT}/logs?stdout=true&stderr=true&tail=50"
    ]

    result = subprocess.run(cmd, capture_output=True, timeout=10)

    if result.returncode != 0:
        logs = f"Error: {result.stderr.decode('utf-8', errors='replace')}"
    else:
        # Docker multiplexed stream: each frame has 8-byte header + payload
        # Header: [stream_type(1), 0, 0, 0, size(4 big-endian)]
        # We strip headers and extract just the text content
        raw = result.stdout
        lines = []
        i = 0
        while i < len(raw):
            if i + 8 > len(raw):
                break
            # Read 4-byte size from header (bytes 4-7, big-endian)
            size = int.from_bytes(raw[i+4:i+8], 'big')
            if i + 8 + size > len(raw):
                break
            payload = raw[i+8:i+8+size]
            try:
                lines.append(payload.decode('utf-8', errors='replace').rstrip())
            except Exception:
                pass
            i += 8 + size
        logs = '\n'.join(lines) if lines else raw.decode('utf-8', errors='replace')

    full_path = APP_DIR / "rails" / output_file
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(logs)
    chown_for_ubuntu(full_path)  # Fix permissions for ubuntu user

    return str(full_path)


def _demultiplex_docker_log_stream(raw: bytes) -> str:
    """Decode Docker's log stream into plain text.

    Docker uses two formats depending on whether the container has a TTY:
    - non-TTY: each frame is 8 bytes [stream_type, 0, 0, 0, size(4 BE)] + payload
    - TTY:     raw stream, no framing
    Auto-detect by looking at the first byte (\\x01 = stdout, \\x02 = stderr).
    """
    if not raw:
        return ""
    if raw[:1] in (b"\x01", b"\x02"):
        parts: list[str] = []
        i = 0
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            if i + 8 + size > len(raw):
                break
            parts.append(raw[i + 8:i + 8 + size].decode("utf-8", errors="replace"))
            i += 8 + size
        return "".join(parts)
    return raw.decode("utf-8", errors="replace")


@tool(description=TAIL_RAILS_LOGS_DESCRIPTION)
def tail_rails_logs(
    runtime: ToolRuntime,
    lines: int = 200,
) -> Command:
    """Read recent stdout/stderr from the Rails container (works on stopped containers)."""
    tool_call_id = runtime.tool_call_id

    try:
        lines = max(1, min(int(lines), 2000))
    except (TypeError, ValueError):
        lines = 200

    try:
        container_name = get_rails_container_name()
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Could not resolve Rails container name: {e}", tool_call_id=tool_call_id)]
            }
        )

    cmd = [
        "curl", "--silent", "--show-error",
        "--unix-socket", "/var/run/docker.sock",
        f"http://localhost/containers/{container_name}/logs?stdout=true&stderr=true&tail={lines}",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        return Command(
            update={
                "messages": [ToolMessage("Timed out reading Rails container logs.", tool_call_id=tool_call_id)]
            }
        )
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Error reading Rails container logs: {e}", tool_call_id=tool_call_id)]
            }
        )

    if result.returncode != 0:
        stderr_text = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else str(result.stderr)
        return Command(
            update={
                "messages": [ToolMessage(f"Docker logs API error (container={container_name}): {stderr_text}", tool_call_id=tool_call_id)]
            }
        )

    logs_text = _demultiplex_docker_log_stream(result.stdout)
    logs_text = truncate_output(logs_text, BASH_OUTPUT_MAX_CHARS)
    if not logs_text.strip():
        logs_text = "(no recent log output)"

    header = f"Recent Rails container logs ({container_name}, last {lines} lines):\n"
    return Command(
        update={
            "messages": [ToolMessage(header + logs_text, tool_call_id=tool_call_id)]
        }
    )


@tool(description=HARD_RESTART_RAILS_DESCRIPTION)
def hard_restart_rails(
    runtime: ToolRuntime,
) -> Command:
    """Forcefully restart the Rails container via the Docker socket.

    Equivalent to `docker compose restart llamapress` — kills the Rails process
    (SIGTERM, then SIGKILL after the timeout) and starts the same container
    fresh. Does NOT recreate the container from docker-compose.yml.
    """
    tool_call_id = runtime.tool_call_id

    try:
        container_name = get_rails_container_name()
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Could not resolve Rails container name: {e}", tool_call_id=tool_call_id)]
            }
        )

    cmd = [
        "curl", "--silent", "--show-error", "--fail-with-body",
        "-X", "POST",
        "--unix-socket", "/var/run/docker.sock",
        f"http://localhost/containers/{container_name}/restart?t=10",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return Command(
            update={
                "messages": [ToolMessage(
                    f"Restart of {container_name} did not complete within 60s. "
                    "The container may still be coming back up — check `tail_rails_logs` in a moment.",
                    tool_call_id=tool_call_id,
                )]
            }
        )
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Error calling Docker restart API: {e}", tool_call_id=tool_call_id)]
            }
        )

    if result.returncode != 0:
        return Command(
            update={
                "messages": [ToolMessage(
                    f"Docker restart API error (container={container_name}): "
                    f"{(result.stderr or result.stdout or '').strip()}",
                    tool_call_id=tool_call_id,
                )]
            }
        )

    return Command(
        update={
            "messages": [ToolMessage(
                f"Hard restart of {container_name} kicked off. The Rails app will be unreachable for a few "
                "seconds while it boots back up. Tell the user the page will reload automatically — do not "
                "ask them to refresh. If you need to verify it came back, wait ~10s, then call "
                "`tail_rails_logs` to confirm Puma logged 'Listening on'.",
                tool_call_id=tool_call_id,
            )]
        }
    )


@tool(description=FIX_PERMISSIONS_DESCRIPTION)
def fix_permissions(
    runtime: ToolRuntime,
) -> Command:
    """Fix file permission issues in the Rails container by chowning problematic directories as root."""
    tool_call_id = runtime.tool_call_id

    try:
        container_name = get_rails_container_name()
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Could not resolve Rails container name: {e}", tool_call_id=tool_call_id)]
            }
        )

    # Exec as root (no User field) to chown directories back to UID 1000
    cmd_str = (
        "chown -R 1000:1000 /rails/tmp /rails/coverage /rails/log 2>/dev/null; "
        "echo 'Permissions fixed successfully'"
    )
    payload = {
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": True,
        "Cmd": ["/bin/sh", "-c", cmd_str],
    }

    # Create exec instance
    create_cmd = [
        "curl", "--silent", "--show-error", "--fail-with-body",
        "--unix-socket", "/var/run/docker.sock",
        "-H", "Content-Type: application/json",
        "--data-binary", json.dumps(payload),
        f"http://localhost/containers/{container_name}/exec",
    ]

    try:
        create_result = subprocess.run(create_cmd, capture_output=True, text=True, timeout=30)
        if create_result.returncode != 0:
            return Command(update={"messages": [ToolMessage(f"Failed to create exec: {create_result.stderr or create_result.stdout}", tool_call_id=tool_call_id)]})

        exec_data = json.loads(create_result.stdout)
        exec_id = exec_data["Id"]

        # Start exec
        start_cmd = [
            "curl", "--silent", "--show-error",
            "--unix-socket", "/var/run/docker.sock",
            "-H", "Content-Type: application/json",
            "--data-binary", json.dumps({"Detach": False, "Tty": True}),
            f"http://localhost/exec/{exec_id}/start",
        ]
        start_result = subprocess.run(start_cmd, capture_output=True, text=True, timeout=60)
        output = start_result.stdout.strip()

        return Command(
            update={
                "messages": [ToolMessage(
                    f"Permission fix completed on {container_name}:\n{output}\n\n"
                    "Directories /rails/tmp, /rails/coverage, and /rails/log have been chowned to 1000:1000. "
                    "You can now retry the command that failed with permission errors.",
                    tool_call_id=tool_call_id,
                )]
            }
        )
    except Exception as e:
        return Command(update={"messages": [ToolMessage(f"Error fixing permissions: {e}", tool_call_id=tool_call_id)]})


@tool(description=BASH_COMMAND_FOR_RAILS_DESCRIPTION)
def bash_command(
    command: str,
    runtime: ToolRuntime,
    workdir: str = WORKDIR,
    timeout_seconds: int = 60,
) -> Command:
    """Execute a bash command in the Rails container."""
    tool_call_id = runtime.tool_call_id
    # Safeguard against secret exfiltration attempts
    forbidden_patterns = [".env", "ENV["]
    for pattern in forbidden_patterns:
        if pattern.lower() in command.lower():
            result = f"Blocked: use of '{pattern}' is not allowed for security reasons. Contact a LlamaPress admin for guidance in retrieving sensitive .env information."
            return Command(
                update={
                    "messages": [
                        ToolMessage(result, tool_call_id=tool_call_id)
                    ],
                }
            )

    raw_result = rails_api_sh(command, workdir, timeout_seconds)

    # Truncate large outputs to prevent context window explosion
    result = truncate_output(raw_result, BASH_OUTPUT_MAX_CHARS)

    # Detect semantic errors in output
    has_error, is_critical, matched_patterns = detect_bash_errors(result)

    # Build the message content
    if is_critical:
        # Add strong guidance for critical errors (permission issues)
        error_guidance = (
            "\n\n<CRITICAL_ERROR>\n"
            f"Detected critical error patterns: {', '.join(matched_patterns[:3])}\n"
            "This is a file permission issue. Use the fix_permissions tool to resolve it, then retry your command.\n"
            "Do NOT try chmod/chown via bash_command — it runs as UID 1000 which can't fix root-owned files.\n"
            "</CRITICAL_ERROR>"
        )
        message_content = f"Command output:\n{result}{error_guidance}"
    elif has_error:
        # Add warning for non-critical errors
        error_warning = f"\n\n[Warning: Detected potential errors: {', '.join(matched_patterns[:3])}]"
        message_content = f"Command output:\n{result}{error_warning}"
    else:
        message_content = f"Command output:\n{result}"

    # Build the update dict
    update = {
        "messages": [
            ToolMessage(message_content, tool_call_id=tool_call_id)
        ],
    }

    # Increment failure counter for errors (this triggers circuit breaker after 3 failures)
    if has_error:
        update["failed_tool_calls_count"] = 1

    return Command(update=update)

# Initialize Tavily client lazily to avoid import errors when API key is not set
_tavily_client = None

def get_tavily_client():
    global _tavily_client
    if _tavily_client is None:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            raise ValueError("TAVILY_API_KEY environment variable is not set")
        _tavily_client = TavilyClient(api_key=api_key)
    return _tavily_client

@tool(description=INTERNET_SEARCH_DESCRIPTION)
def internet_search(
    query: str,
    max_results: int = 5,
    include_raw_content: bool = False,
):
    try:
        return get_tavily_client().search(
            query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            topic="general",
        )
    except Exception as e:
        return f"Search unavailable: {e}. The TAVILY_API_KEY may be missing or invalid — ask the operator to configure it."

@tool(description=GIT_STATUS_DESCRIPTION)
def git_status(
    runtime: ToolRuntime,
) -> Command:
    """Get the status of the git repository."""
    tool_call_id = runtime.tool_call_id

    def run_git(cmd: str) -> tuple[str, str | None]:
        """Run a git command. Returns (output, error) tuple. If error is not None, command failed."""
        try:
            result = subprocess.run(
                ["/bin/sh", "-lc", f"git -C /app/leonardo {cmd}"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode != 0:
                return "", f"Git command failed: {cmd}\n{result.stderr}"
            return result.stdout.strip(), None
        except subprocess.TimeoutExpired:
            return "", f"Git command timed out: {cmd}"
        except Exception as e:
            return "", f"Git command error: {cmd}\n{str(e)}"

    def parse_git_status(status_output: str) -> list:
        """Parse git status --porcelain=v2 output into structured file changes."""
        files = []
        lines = status_output.splitlines()
        
        for line in lines:
            if line.startswith('# '):
                continue  # Skip branch info
            
            if line.startswith('1 ') or line.startswith('2 '):
                # Format: 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
                # or:     2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path><sep><origPath>
                parts = line.split(' ', 8)
                if len(parts) >= 9:
                    xy = parts[1]  # Status codes
                    path = parts[8]
                    
                    # Handle renames (type 2)
                    if line.startswith('2 '):
                        # For renames, path contains both old and new names
                        if '\t' in path:
                            new_path, old_path = path.split('\t', 1)
                            path = f"{old_path} → {new_path}"
                    
                    # Map status codes to readable names
                    status_map = {
                        'M.': 'Modified',
                        '.M': 'Modified (worktree)',
                        'MM': 'Modified (both)',
                        'A.': 'Added',
                        '.A': 'Added (worktree)',
                        'AA': 'Added (both)',
                        'D.': 'Deleted',
                        '.D': 'Deleted (worktree)',
                        'DD': 'Deleted (both)',
                        'R.': 'Renamed',
                        '.R': 'Renamed (worktree)',
                        'C.': 'Copied',
                        '.C': 'Copied (worktree)',
                        'U.': 'Unmerged',
                        '.U': 'Unmerged (worktree)',
                        '??': 'Untracked'
                    }
                    
                    status_desc = status_map.get(xy, f'Unknown ({xy})')
                    files.append({
                        'path': path,
                        'status': status_desc,
                        'status_code': xy
                    })
            elif line.startswith('? '):
                # Untracked file
                path = line[2:]  # Remove '? ' prefix
                files.append({
                    'path': path,
                    'status': 'Untracked',
                    'status_code': '??'
                })
        
        return files
    
    # Collect data
    status, status_err = run_git("status --porcelain=v2 --branch")
    if status_err:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        f"Error getting git status: {status_err}",
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )

    log, log_err = run_git(
        "log -n 10 --pretty=format:'{\"hash\":\"%H\",\"author\":\"%an\",\"date\":\"%ad\",\"subject\":\"%s\"},'"
    )
    if log_err:
        # Log errors are non-fatal, just use empty log
        log = ""

    # Parse changed files
    changed_files = parse_git_status(status)
    
    # Get individual diffs for each changed file
    for file_info in changed_files:
        file_path = file_info['path']
        
        # Skip untracked files for diff (they don't have diffs)
        if file_info['status_code'] == '??':
            file_info['diff'] = f"New file: {file_path}"
            continue
        
        # Handle renamed files
        if '→' in file_path:
            # For renames, get diff of the new file name
            new_path = file_path.split(' → ')[1]
            diff_output, diff_err = run_git(f"diff HEAD -- '{new_path}'")
            if diff_err or not diff_output:
                # If no diff with HEAD, try staged diff
                diff_output, _ = run_git(f"diff --cached -- '{new_path}'")
            if not diff_output:
                diff_output = f"Could not get diff for renamed file: {file_path}"
        else:
            # Try to get diff for the file
            diff_output, diff_err = run_git(f"diff HEAD -- '{file_path}'")
            if diff_err or not diff_output:
                # If no diff with HEAD, try staged diff
                diff_output, _ = run_git(f"diff --cached -- '{file_path}'")
            if (not diff_output) and file_info['status_code'].endswith('M'):
                # For worktree modifications, try diff without HEAD
                diff_output, _ = run_git(f"diff -- '{file_path}'")
            if not diff_output:
                diff_output = f"Could not get diff for file: {file_path}"

        file_info['diff'] = diff_output if diff_output else f"No changes to display for {file_path}"

    # Parse log into JSON
    log_json = "[" + log.strip().rstrip(",") + "]"
    commits = json.loads(log_json) if log_json.strip("[]") else []
    
    # Pre-fetch commit diffs for all commits
    commit_diffs = {}
    for commit in commits:
        commit_hash = commit['hash']
        # Get commit diff
        diff_output, diff_err = run_git(f"show --no-merges {commit_hash}")
        if diff_err:
            # If we can't get the diff for a commit, skip it but don't fail
            print(f"Warning: Could not get diff for commit {commit_hash}: {diff_err}")
            continue
        try:
            
            # Parse the diff into structured data using the same function from get_commit_diff
            def parse_commit_diff(diff_output: str) -> list:
                """Parse git diff output into structured file changes."""
                files = []
                current_file = None
                current_diff_lines = []
                
                lines = diff_output.splitlines()
                
                for line in lines:
                    if line.startswith('diff --git'):
                        # Save previous file if exists
                        if current_file:
                            current_file['diff'] = '\n'.join(current_diff_lines)
                            files.append(current_file)
                        
                        # Start new file
                        # Extract file paths from "diff --git a/path b/path"
                        parts = line.split(' ')
                        if len(parts) >= 4:
                            old_path = parts[2][2:]  # Remove 'a/' prefix
                            new_path = parts[3][2:]  # Remove 'b/' prefix
                            
                            current_file = {
                                'path': new_path if new_path != '/dev/null' else old_path,
                                'old_path': old_path if old_path != '/dev/null' else None,
                                'new_path': new_path if new_path != '/dev/null' else None,
                                'status': 'Modified',
                                'status_code': 'M.'
                            }
                            current_diff_lines = [line]
                        else:
                            current_diff_lines = [line]
                    elif line.startswith('new file mode'):
                        if current_file:
                            current_file['status'] = 'Added'
                            current_file['status_code'] = 'A.'
                        current_diff_lines.append(line)
                    elif line.startswith('deleted file mode'):
                        if current_file:
                            current_file['status'] = 'Deleted'
                            current_file['status_code'] = 'D.'
                        current_diff_lines.append(line)
                    elif line.startswith('rename from') or line.startswith('rename to'):
                        if current_file:
                            current_file['status'] = 'Renamed'
                            current_file['status_code'] = 'R.'
                        current_diff_lines.append(line)
                    else:
                        current_diff_lines.append(line)
                
                # Don't forget the last file
                if current_file:
                    current_file['diff'] = '\n'.join(current_diff_lines)
                    files.append(current_file)
                
                return files
            
            commit_files = parse_commit_diff(diff_output)
            commit_diffs[commit_hash] = {
                'files': commit_files,
                'subject': commit['subject'],
                'author': commit['author'],
                'date': commit['date']
            }
        except Exception as e:
            # If we can't get the diff for a commit, skip it but don't fail
            print(f"Warning: Could not get diff for commit {commit_hash}: {e}")
            continue

    def format_diff(diff_text: str) -> str:
        """Format diff text with HTML classes for syntax highlighting."""
        if not diff_text:
            return ""
        
        lines = diff_text.splitlines()
        formatted_lines = []
        
        for line in lines:
            if line.startswith('+++') or line.startswith('---'):
                formatted_lines.append(f'<span class="diff-header">{line}</span>')
            elif line.startswith('@@'):
                formatted_lines.append(f'<span class="diff-header">{line}</span>')
            elif line.startswith('+'):
                formatted_lines.append(f'<span class="diff-line-add">{line}</span>')
            elif line.startswith('-'):
                formatted_lines.append(f'<span class="diff-line-remove">{line}</span>')
            else:
                formatted_lines.append(f'<span class="diff-line-context">{line}</span>')
        
        return '\n'.join(formatted_lines)

    return Command(
        update={
            "messages": [
                ToolMessage(
                    f"Git status:\n{status}\n\nChanged files: {len(changed_files)}\nCommits:\n{json.dumps(commits, indent=2)}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )

@tool(description=GIT_COMMIT_DESCRIPTION)
def git_commit(
    message: str,
    runtime: ToolRuntime,
) -> Command:
    """Commit the changes to the git repository."""
    tool_call_id = runtime.tool_call_id
    # First add all changes
    add_result = subprocess.run(["/bin/sh", "-lc", "git -C /app/leonardo add ."], capture_output=True, text=True, timeout=30)

    # Then commit the changes - use subprocess list format to avoid shell escaping issues
    commit_result = subprocess.run(["git", "-C", "/app/leonardo", "commit", "-m", message], capture_output=True, text=True, timeout=30)

    # Combine the output from both commands
    output = f"Git add:\n{add_result.stdout}"
    if add_result.stderr:
        output += f"\nGit add errors:\n{add_result.stderr}"

    output += f"\n\nGit commit:\n{commit_result.stdout}"
    if commit_result.stderr:
        output += f"\nGit commit errors:\n{commit_result.stderr}"

    # After committing, automatically run git status to show the current state
    try:
        status_command = git_status(runtime)
        # Extract the git status message from the status command
        status_messages = status_command.update.get("messages", [])
        if status_messages:
            output += f"\n\n--- Post-commit Git Status ---\n{status_messages[0].content}"
    except Exception as e:
        output += f"\n\nError getting post-commit git status: {str(e)}"

    return Command(
        update={
            "messages": [ToolMessage(output, tool_call_id=tool_call_id)],
        }
    )

@tool(description=GIT_COMMAND_DESCRIPTION)
def git_command(
    command: str,
    runtime: ToolRuntime,
) -> Command:
    """Execute a git command in the app repository."""
    tool_call_id = runtime.tool_call_id
    git_result = subprocess.run(["/bin/sh", "-lc", f"git -C /app/leonardo {command}"], capture_output=True, text=True, timeout=30)

    output = f"Git command:\n{command}\n\nGit result:\n{git_result.stdout}"
    if git_result.stderr:
        output += f"\nGit command errors:\n{git_result.stderr}"

    return Command(
        update={
            "messages": [ToolMessage(output, tool_call_id=tool_call_id)],
        }
    )

@tool(description=GITHUB_CLI_DESCRIPTION)
def github_cli_command(
    command: str,
    runtime: ToolRuntime,
) -> Command:
    """Use the github cli to do anything else related specifically to github."""
    tool_call_id = runtime.tool_call_id
    github_result = subprocess.run(["/bin/sh", "-lc", f"gh {command}"], capture_output=True, text=True, timeout=30)

    output = f"Github result:\n{github_result.stdout}"
    if github_result.stderr:
        output += f"\nGithub command errors:\n{github_result.stderr}"

    return Command(
        update={
            "messages": [ToolMessage(output, tool_call_id=tool_call_id)],
        }
    )

# ============================================================================
# AGENT FILE TOOLS - For creating/editing LangGraph agents in user_agents/
# ============================================================================

@tool(description="""List all custom agents in the user_agents directory.
Returns a list of agent names (directory names) found in /app/app/user_agents/.
This helps you see what custom agents have been created.""")
def ls_agents() -> str:
    """List all custom agents in the user_agents directory."""
    user_agents_dir = APP_DIR / "user_agents"

    if not user_agents_dir.exists():
        return "Error: user_agents directory not found"

    try:
        # Get all directories (agents) in user_agents/
        agents = [d.name for d in user_agents_dir.iterdir() if d.is_dir() and not d.name.startswith('.')]

        if not agents:
            return "No custom agents found in user_agents directory"

        return f"Custom agents found:\n" + "\n".join(f"  - {agent}" for agent in sorted(agents))
    except Exception as e:
        return f"Error listing agents: {e}"

@tool(description="""Read a custom agent's nodes.py file.
Usage:
- agent_name: The name of the agent to read (e.g., 'leo', 'student')
Returns the contents of /app/app/user_agents/{agent_name}/nodes.py with line numbers.""")
def read_agent_file(
    agent_name: str,
    runtime: ToolRuntime,
) -> str:
    """Read a custom agent's nodes.py file."""
    # Construct the full path
    full_path = APP_DIR / "user_agents" / agent_name / "nodes.py"

    # Check if file exists
    if not full_path.exists():
        return f"Error: Agent file not found at user_agents/{agent_name}/nodes.py"

    # Read the file contents
    try:
        content = full_path.read_text()
    except Exception as e:
        return f"Error reading agent file: {e}"

    # Handle empty file
    if not content or content.strip() == "":
        return "System reminder: File exists but has empty contents"

    # Split content into lines
    lines = content.splitlines()

    # Format output with line numbers (cat -n format)
    result_lines = []
    for i, line_content in enumerate(lines):
        # Truncate lines longer than 2000 characters
        if len(line_content) > 2000:
            line_content = line_content[:2000]

        # Line numbers start at 1
        line_number = i + 1
        result_lines.append(f"{line_number:6d}\t{line_content}")

    return "\n".join(result_lines)

@tool(description="""Create or overwrite a custom agent's nodes.py file.
Usage:
- agent_name: The name of the agent (e.g., 'leo', 'student'). This will create user_agents/{agent_name}/nodes.py
- file_content: The complete Python code for the agent's nodes.py file. Must include build_workflow() function.
This tool will create the agent directory if it doesn't exist.""")
def write_agent_file(
    agent_name: str,
    file_content: str,
    runtime: ToolRuntime,
) -> Command:
    """Create or overwrite a custom agent's nodes.py file."""
    tool_call_id = runtime.tool_call_id
    # Construct the full path
    agent_dir = APP_DIR / "user_agents" / agent_name
    full_path = agent_dir / "nodes.py"

    try:
        # Create agent directory if it doesn't exist
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Basic Python syntax validation
        try:
            compile(file_content, f"user_agents/{agent_name}/nodes.py", 'exec')
        except SyntaxError as e:
            error_message = f"Python syntax error in agent file: {e}"
            tool_output = {
                "status": "error",
                "message": error_message
            }
            return Command(
                update={
                    "messages": [
                        ToolMessage(
                            error_message,
                            artifact=tool_output,
                            tool_call_id=tool_call_id,
                        )
                    ]
                }
            )

        # Write the file
        full_path.write_text(file_content)
        chown_for_ubuntu(full_path)  # Fix permissions for ubuntu user

    except Exception as e:
        error_message = f"Error writing agent file {agent_name}/nodes.py: {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        error_message,
                        artifact=tool_output,
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    success_message = f"Created/updated agent file user_agents/{agent_name}/nodes.py"
    tool_output = {
        "status": "success",
        "message": success_message
    }

    return Command(
        update={
            "messages": [
                ToolMessage(success_message, artifact=tool_output, tool_call_id=tool_call_id)
            ],
        }
    )

@tool(description="""Edit a custom agent's nodes.py file by replacing text.
Usage:
- agent_name: The name of the agent to edit (e.g., 'leo', 'student')
- old_string: The exact text to find and replace
- new_string: The text to replace it with
- replace_all: If True, replace all occurrences. If False (default), only replace first occurrence and error if not unique.
The old_string must exist in the file or this will fail.""")
def edit_agent_file(
    agent_name: str,
    old_string: str,
    new_string: str,
    runtime: ToolRuntime,
    replace_all: bool = False,
) -> Command:
    """Edit a custom agent's nodes.py file by replacing text."""
    tool_call_id = runtime.tool_call_id
    full_path = APP_DIR / "user_agents" / agent_name / "nodes.py"

    if not full_path.exists():
        error_message = f"Error: Agent file not found at user_agents/{agent_name}/nodes.py"
        tool_output = {
            "status": "error",
            "message": error_message
        }

        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )

    try:
        original_content = full_path.read_text()
    except Exception as e:
        error_message = f"Error reading agent file: {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )

    # Check if old_string exists
    if old_string not in original_content:
        # Try with normalized whitespace
        if normalize_whitespace(old_string) not in normalize_whitespace(original_content):
            error_message = f"Error: Could not find the specified text in user_agents/{agent_name}/nodes.py"
            tool_output = {
                "status": "error",
                "message": error_message
            }
            return Command(
                update={
                    "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
                }
            )
        # If normalized version matches, use it
        normalized_content = normalize_whitespace(original_content)
        normalized_old = normalize_whitespace(old_string)
        normalized_new = normalize_whitespace(new_string)
        new_content = normalized_content.replace(normalized_old, normalized_new, 1 if not replace_all else -1)
    else:
        # Check if old_string is unique (only if not replace_all)
        if not replace_all and original_content.count(old_string) > 1:
            error_message = f"Error: The text to replace appears {original_content.count(old_string)} times in the file. Please provide a more specific string or use replace_all=True"
            tool_output = {
                "status": "error",
                "message": error_message
            }
            return Command(
                update={
                    "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
                }
            )

        # Perform the replacement
        if replace_all:
            new_content = original_content.replace(old_string, new_string)
        else:
            new_content = original_content.replace(old_string, new_string, 1)

    # Basic Python syntax validation
    try:
        compile(new_content, f"user_agents/{agent_name}/nodes.py", 'exec')
    except SyntaxError as e:
        error_message = f"Edit would create Python syntax error: {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        error_message,
                        artifact=tool_output,
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    # Write the new content
    try:
        full_path.write_text(new_content)
        chown_for_ubuntu(full_path)  # Fix permissions for ubuntu user
    except Exception as e:
        error_message = f"Error writing agent file: {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )

    # Generate a simple diff for the user
    old_lines = original_content.splitlines()
    new_lines = new_content.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm='', fromfile='before', tofile='after'))
    diff_output = '\n'.join(diff[:50])  # Limit diff output

    success_message = f"Successfully edited user_agents/{agent_name}/nodes.py"
    if diff:
        success_message += f"\n\nDiff preview:\n{diff_output}"

    tool_output = {
        "status": "success",
        "message": success_message
    }

    return Command(
        update={
            "messages": [ToolMessage(success_message, artifact=tool_output, tool_call_id=tool_call_id)]
        }
    )

# ============================================================================
# MEMORY TOOLS - Persistent memory across conversations
# ============================================================================

@tool(description=SAVE_MEMORY_DESCRIPTION)
def save_memory(
    name: str,
    description: str,
    memory_type: str,
    content: str,
    runtime: ToolRuntime,
) -> Command:
    """Save information to long-term memory."""
    tool_call_id = runtime.tool_call_id

    try:
        filename = write_memory_file(name, description, memory_type, content)
        success_message = f"Memory saved: {filename}"
        return Command(
            update={
                "messages": [ToolMessage(success_message, tool_call_id=tool_call_id)]
            }
        )
    except ValueError as e:
        error_message = f"Error saving memory: {e}"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )
    except Exception as e:
        error_message = f"Unexpected error saving memory: {e}"
        return Command(
            update={
                "messages": [ToolMessage(error_message, tool_call_id=tool_call_id)]
            }
        )


@tool(description=LIST_MEMORIES_DESCRIPTION)
def list_memories(
    runtime: ToolRuntime,
) -> Command:
    """List all saved memories."""
    tool_call_id = runtime.tool_call_id

    memories = list_all_memories()

    if not memories:
        return Command(
            update={
                "messages": [ToolMessage("No memories saved yet.", tool_call_id=tool_call_id)]
            }
        )

    lines = [f"Found {len(memories)} saved memories:\n"]
    for mem in memories:
        lines.append(f"### {mem['name']} (type: {mem['type']}, file: {mem['filename']})")
        lines.append(f"_{mem['description']}_")
        lines.append(f"{mem['content']}\n")

    return Command(
        update={
            "messages": [ToolMessage("\n".join(lines), tool_call_id=tool_call_id)]
        }
    )


@tool(description=DELETE_MEMORY_DESCRIPTION)
def delete_memory(
    filename: str,
    runtime: ToolRuntime,
) -> Command:
    """Delete a memory by filename."""
    tool_call_id = runtime.tool_call_id

    if delete_memory_file(filename):
        return Command(
            update={
                "messages": [ToolMessage(f"Memory deleted: {filename}", tool_call_id=tool_call_id)]
            }
        )
    else:
        return Command(
            update={
                "messages": [ToolMessage(f"Memory not found: {filename}", tool_call_id=tool_call_id)]
            }
        )


# ============================================================================
# LEONARDO.MD TOOLS - Read/edit the project context file
# ============================================================================

@tool(description=READ_LEONARDO_MD_DESCRIPTION)
def read_leonardo_md(
    runtime: ToolRuntime,
) -> Command:
    """Read the LEONARDO.md project context file."""
    tool_call_id = runtime.tool_call_id
    filepath = Path(LEONARDO_MD_PATH)

    if not filepath.exists():
        return Command(
            update={
                "messages": [ToolMessage("LEONARDO.md does not exist yet. Use write_leonardo_md to create it.", tool_call_id=tool_call_id)]
            }
        )

    try:
        content = filepath.read_text(encoding="utf-8")
        if not content.strip():
            return Command(
                update={
                    "messages": [ToolMessage("LEONARDO.md exists but is empty.", tool_call_id=tool_call_id)]
                }
            )

        # Add line numbers
        lines = content.splitlines()
        numbered = [f"{i+1:6d}\t{line}" for i, line in enumerate(lines)]
        result = f"Contents of LEONARDO.md ({len(lines)} lines):\n\n" + "\n".join(numbered)

        return Command(
            update={
                "messages": [ToolMessage(result, tool_call_id=tool_call_id)]
            }
        )
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Error reading LEONARDO.md: {e}", tool_call_id=tool_call_id)]
            }
        )


@tool(description=EDIT_LEONARDO_MD_DESCRIPTION)
def edit_leonardo_md(
    old_string: str,
    new_string: str,
    runtime: ToolRuntime,
) -> Command:
    """Edit the LEONARDO.md project context file by replacing text."""
    tool_call_id = runtime.tool_call_id
    filepath = Path(LEONARDO_MD_PATH)

    if not filepath.exists():
        return Command(
            update={
                "messages": [ToolMessage("Error: LEONARDO.md does not exist. Use write_leonardo_md to create it.", tool_call_id=tool_call_id)]
            }
        )

    try:
        content = filepath.read_text(encoding="utf-8")
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Error reading LEONARDO.md: {e}", tool_call_id=tool_call_id)]
            }
        )

    if old_string not in content:
        return Command(
            update={
                "messages": [ToolMessage("Error: Could not find the specified text in LEONARDO.md. Read the file first to see exact contents.", tool_call_id=tool_call_id)]
            }
        )

    if content.count(old_string) > 1:
        return Command(
            update={
                "messages": [ToolMessage(f"Error: The text to replace appears {content.count(old_string)} times. Provide more context to make it unique.", tool_call_id=tool_call_id)]
            }
        )

    new_content = content.replace(old_string, new_string, 1)
    filepath.write_text(new_content, encoding="utf-8")

    return Command(
        update={
            "messages": [ToolMessage("Successfully edited LEONARDO.md.", tool_call_id=tool_call_id)]
        }
    )


@tool(description=WRITE_LEONARDO_MD_DESCRIPTION)
def write_leonardo_md(
    content: str,
    runtime: ToolRuntime,
) -> Command:
    """Create or overwrite the LEONARDO.md project context file."""
    tool_call_id = runtime.tool_call_id
    filepath = Path(LEONARDO_MD_PATH)

    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        return Command(
            update={
                "messages": [ToolMessage(f"Successfully wrote LEONARDO.md ({len(content)} chars).", tool_call_id=tool_call_id)]
            }
        )
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Error writing LEONARDO.md: {e}", tool_call_id=tool_call_id)]
            }
        )


# ============================================================================
# BRAND GUIDE TOOLS - read/write the project's brand.json (colors, logos, notes)
# ============================================================================
# These share app.services.brand_service with the /api/brand endpoint, so a
# change from Leo and a change from the toolbar editor go through the exact same
# write path (brand.json + BRAND.md + brand-guidelines skill).


@tool(description=READ_BRAND_GUIDE_DESCRIPTION)
def read_brand_guide(runtime: ToolRuntime) -> Command:
    """Read the project's Brand Guide (colors, logos, notes)."""
    import json
    tool_call_id = runtime.tool_call_id
    try:
        brand = load_brand()
        raw = json.dumps(brand, indent=2, ensure_ascii=False)
        result = (
            f"{render_brand_md(brand)}\n\n---\n\n"
            "Structured brand.json (edit with write_brand_guide — to change one "
            "color/logo, pass the FULL list back with your edit applied):\n\n"
            f"```json\n{raw}\n```"
        )
        return Command(update={"messages": [ToolMessage(result, tool_call_id=tool_call_id)]})
    except Exception as e:
        return Command(update={"messages": [ToolMessage(f"Error reading brand guide: {e}", tool_call_id=tool_call_id)]})


@tool(description=WRITE_BRAND_GUIDE_DESCRIPTION)
def write_brand_guide(
    runtime: ToolRuntime,
    colors: Optional[list] = None,
    logos: Optional[list] = None,
    notes: Optional[str] = None,
) -> Command:
    """Create/update the Brand Guide. Provided sections replace; omitted are kept."""
    tool_call_id = runtime.tool_call_id

    if colors is None and logos is None and notes is None:
        return Command(update={"messages": [ToolMessage(
            "No changes provided. Pass at least one of: colors, logos, or notes.",
            tool_call_id=tool_call_id)]})

    try:
        brand = load_brand()
        if colors is not None:
            brand["colors"] = colors
        if logos is not None:
            brand["logos"] = logos
        if notes is not None:
            brand["notes"] = notes

        saved = save_brand(brand)
        summary = (
            f"Brand guide saved: {len(saved['colors'])} color(s), "
            f"{len(saved['logos'])} logo(s). BRAND.md and the brand-guidelines "
            "skill were refreshed; the change applies on the next turn."
        )
        palette = ", ".join(
            f"{c.get('name') or 'Color'} {c.get('hex')}"
            for c in saved["colors"] if c.get("hex")
        )
        if palette:
            summary += f"\nColors: {palette}."
        return Command(update={"messages": [ToolMessage(summary, tool_call_id=tool_call_id)]})
    except Exception as e:
        return Command(update={"messages": [ToolMessage(f"Error saving brand guide: {e}", tool_call_id=tool_call_id)]})


# ============================================================================
# SKILL TOOLS - Agent Skills (SKILL.md progressive disclosure)
# ============================================================================
# use_skill loads a skill's full instructions into the conversation on demand;
# the management tools (list/read/write/edit/delete) let the agent curate the
# .leonardo/skills/ library the same way it manages memories and LEONARDO.md.


def build_use_skill_tool():
    """Build the use_skill tool with the current <available_skills> catalog baked
    into its description.

    The description is dynamic — it lists every installed skill's slug +
    description so the model can decide when to fire. Compiled graphs are cached
    at startup, so RefreshSkillCatalogMiddleware (agent_factory.py) rebuilds this
    tool per request to keep the catalog live as skills are authored/deleted.
    """
    description = USE_SKILL_DESCRIPTION_TEMPLATE.format(
        available_skills=render_available_skills()
    )

    @tool("use_skill", description=description)
    def use_skill(slug: str, runtime: ToolRuntime) -> Command:
        """Load a skill's full instructions into context."""
        tool_call_id = runtime.tool_call_id
        body = get_skill_body(slug)
        if body is None:
            available = [s["slug"] for s in list_all_skills()]
            hint = ", ".join(available) if available else "(none installed)"
            return Command(
                update={
                    "messages": [ToolMessage(
                        f"No skill '{slug}' found. Available skills: {hint}",
                        tool_call_id=tool_call_id,
                    )]
                }
            )
        return Command(
            update={
                "messages": [ToolMessage(body, tool_call_id=tool_call_id)]
            }
        )

    return use_skill


@tool(description=LIST_SKILLS_DESCRIPTION)
def list_skills(
    runtime: ToolRuntime,
) -> Command:
    """List all installed skills."""
    tool_call_id = runtime.tool_call_id

    skills = list_all_skills()
    if not skills:
        return Command(
            update={
                "messages": [ToolMessage("No skills installed yet. Use write_skill to create one.", tool_call_id=tool_call_id)]
            }
        )

    lines = [f"Found {len(skills)} skill(s):\n"]
    for s in skills:
        lines.append(f"- {s['slug']} — {s['name']}")
        lines.append(f"  {s['description'] or '(no description)'}")

    return Command(
        update={
            "messages": [ToolMessage("\n".join(lines), tool_call_id=tool_call_id)]
        }
    )


@tool(description=READ_SKILL_DESCRIPTION)
def read_skill(
    slug: str,
    runtime: ToolRuntime,
) -> Command:
    """Read a skill's raw SKILL.md source with line numbers."""
    tool_call_id = runtime.tool_call_id

    body = get_skill_body(slug)
    if body is None:
        return Command(
            update={
                "messages": [ToolMessage(f"Skill '{slug}' does not exist. Use list_skills to see available skills.", tool_call_id=tool_call_id)]
            }
        )

    lines = body.splitlines()
    numbered = [f"{i+1:6d}\t{line}" for i, line in enumerate(lines)]
    result = f"Contents of .leonardo/skills/{slug}/SKILL.md ({len(lines)} lines):\n\n" + "\n".join(numbered)

    return Command(
        update={
            "messages": [ToolMessage(result, tool_call_id=tool_call_id)]
        }
    )


@tool(description=WRITE_SKILL_DESCRIPTION)
def write_skill(
    name: str,
    description: str,
    content: str,
    runtime: ToolRuntime,
    slug: Optional[str] = None,
) -> Command:
    """Create or overwrite a skill's SKILL.md."""
    tool_call_id = runtime.tool_call_id

    try:
        saved_slug = write_skill_file(name, description, content, slug=slug)
        return Command(
            update={
                "messages": [ToolMessage(f"Skill saved: {saved_slug} (.leonardo/skills/{saved_slug}/SKILL.md)", tool_call_id=tool_call_id)]
            }
        )
    except ValueError as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Error saving skill: {e}", tool_call_id=tool_call_id)]
            }
        )
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Unexpected error saving skill: {e}", tool_call_id=tool_call_id)]
            }
        )


@tool(description=EDIT_SKILL_DESCRIPTION)
def edit_skill(
    slug: str,
    old_string: str,
    new_string: str,
    runtime: ToolRuntime,
) -> Command:
    """Edit a skill's SKILL.md by replacing text."""
    tool_call_id = runtime.tool_call_id

    try:
        edit_skill_file(slug, old_string, new_string)
        return Command(
            update={
                "messages": [ToolMessage(f"Successfully edited skill '{slug}'.", tool_call_id=tool_call_id)]
            }
        )
    except ValueError as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Error editing skill: {e}", tool_call_id=tool_call_id)]
            }
        )


@tool(description=DELETE_SKILL_DESCRIPTION)
def delete_skill(
    slug: str,
    runtime: ToolRuntime,
) -> Command:
    """Delete a skill by slug."""
    tool_call_id = runtime.tool_call_id

    if delete_skill_file(slug):
        return Command(
            update={
                "messages": [ToolMessage(f"Skill deleted: {slug}", tool_call_id=tool_call_id)]
            }
        )
    return Command(
        update={
            "messages": [ToolMessage(f"Skill not found: {slug}", tool_call_id=tool_call_id)]
        }
    )


VALID_PERSONALITY_FILES = {
    "SOUL.md": SOUL_MD_PATH,
    "USER.md": USER_MD_PATH,
    "IDENTITY.md": IDENTITY_MD_PATH,
}


@tool(description="""Write a personality file (SOUL.md, USER.md, or IDENTITY.md) to the .leonardo/ workspace.
These files define the agent's identity, personality, and knowledge about the user.
Use this when updating personality/user info over time as you learn about the user.
- filename: Must be one of: SOUL.md, USER.md, IDENTITY.md
- content: The markdown content to write""")
def write_personality_file(
    filename: str,
    content: str,
    runtime: ToolRuntime,
) -> Command:
    """Write a personality file to .leonardo/."""
    tool_call_id = runtime.tool_call_id

    if filename not in VALID_PERSONALITY_FILES:
        return Command(
            update={
                "messages": [ToolMessage(
                    f"Error: filename must be one of: {', '.join(VALID_PERSONALITY_FILES.keys())}",
                    tool_call_id=tool_call_id
                )]
            }
        )

    filepath = Path(VALID_PERSONALITY_FILES[filename])
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")
        return Command(
            update={
                "messages": [ToolMessage(f"Successfully wrote {filename} ({len(content)} chars).", tool_call_id=tool_call_id)]
            }
        )
    except Exception as e:
        return Command(
            update={
                "messages": [ToolMessage(f"Error writing {filename}: {e}", tool_call_id=tool_call_id)]
            }
        )

@tool(description="""Read the agent registry.
Returns the MERGED list of all registered agents (platform base langgraph.json ∪ the
client overlay langgraph.local.json), then the raw contents of langgraph.local.json —
the ONLY file you may edit. Register new client agents by editing the overlay; the
platform base is read-only and is overwritten by platform updates.""")
def read_langgraph_json(
    runtime: ToolRuntime,
) -> str:
    """Read the merged agent registry + the editable client overlay."""
    from app.lib.langgraph_registry import load_graphs, local_overlay_path
    base_path = APP_DIR / "langgraph.json"

    try:
        merged = load_graphs(base_path)
        merged_list = "\n".join(f"  - {name}" for name in sorted(merged)) or "  (none)"
    except Exception as e:
        merged_list = f"  (could not read merged registry: {e})"

    overlay_path = local_overlay_path(base_path)
    if overlay_path.exists():
        try:
            overlay_content = overlay_path.read_text()
        except Exception as e:
            overlay_content = f"(error reading overlay: {e})"
    else:
        overlay_content = (
            '(does not exist yet — edit_langgraph_json will create it as:\n'
            '{\n  "graphs": {}\n}\n)'
        )

    return (
        f"All registered agents (platform base ∪ client overlay):\n{merged_list}\n\n"
        f"Editable client overlay — {overlay_path} (register new agents HERE):\n\n"
        f"{overlay_content}"
    )

@tool(description="""Register/edit agents in the CLIENT overlay (langgraph.local.json).
Usage:
- old_string: The exact JSON text to find and replace in langgraph.local.json
- new_string: The JSON text to replace it with
Adds/edits entries in the overlay's "graphs" object. The overlay is created as
{"graphs": {}} if it doesn't exist yet — to add the first agent, use
old_string='"graphs": {}' and new_string='"graphs": { "my_agent": "..." }'.
NOTE: this edits ONLY the client overlay, never the platform base langgraph.json
(the base is read-only and overwritten by platform updates). Client agents registered
here survive container recreates and platform syncs.""")
def edit_langgraph_json(
    old_string: str,
    new_string: str,
    runtime: ToolRuntime,
) -> Command:
    """Edit the client overlay langgraph.local.json (creating it if absent)."""
    from app.lib.langgraph_registry import local_overlay_path
    tool_call_id = runtime.tool_call_id
    base_path = APP_DIR / "langgraph.json"
    full_path = local_overlay_path(base_path)

    # The overlay is optional by design — materialize an empty one on first write so the
    # AI-builder always has a valid target and never has to touch the platform base.
    if not full_path.exists():
        try:
            full_path.write_text('{\n  "graphs": {}\n}\n')
            chown_for_ubuntu(full_path)
        except Exception as e:
            error_message = f"Error creating client overlay {full_path}: {e}"
            tool_output = {
                "status": "error",
                "message": error_message
            }
            return Command(
                update={
                    "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
                }
            )

    try:
        original_content = full_path.read_text()
    except Exception as e:
        error_message = f"Error reading langgraph.json: {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )

    # Check if old_string exists
    if old_string not in original_content:
        error_message = (
            "Error: Could not find the specified text in langgraph.local.json. "
            "Note you can only edit the client overlay, not the platform base — "
            "call read_langgraph_json to see the overlay's current contents."
        )
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )

    # Perform the replacement (only first occurrence for safety)
    new_content = original_content.replace(old_string, new_string, 1)

    # Validate JSON syntax
    try:
        json.loads(new_content)
    except json.JSONDecodeError as e:
        error_message = f"Edit would create invalid JSON: {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        error_message,
                        artifact=tool_output,
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    # Write the new content
    try:
        full_path.write_text(new_content)
        chown_for_ubuntu(full_path)  # Fix permissions for ubuntu user
    except Exception as e:
        error_message = f"Error writing langgraph.json: {e}"
        tool_output = {
            "status": "error",
            "message": error_message
        }
        return Command(
            update={
                "messages": [ToolMessage(error_message, artifact=tool_output, tool_call_id=tool_call_id)]
            }
        )

    success_message = "Successfully edited langgraph.local.json (client overlay)"
    tool_output = {
        "status": "success",
        "message": success_message,
        "new_content": new_content
    }

    return Command(
        update={
            "messages": [ToolMessage(success_message, artifact=tool_output, tool_call_id=tool_call_id)]
        }
    )


def browser_inspect_enabled() -> bool:
    """Whether the headless-browser ``browser_inspect`` tool is enabled for this instance.

    Gated by the ``enable_browser_inspect`` site setting, which defaults to ``"false"``
    so the Playwright/Chromium path is opt-in (disabled unless explicitly configured).
    Read when an agent's tool list is built (workflow compile time), so flipping the
    setting takes effect on the next workflow rebuild / app restart. Fails closed
    (returns ``False``) when the auth DB is unavailable.
    """
    import logging
    try:
        from sqlmodel import Session
        from app.db import engine
        from app.routers.api import get_site_setting
        if engine is None:
            return False
        with Session(engine) as session:
            return get_site_setting(session, "enable_browser_inspect", "false") == "true"
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"Could not read enable_browser_inspect setting; tool disabled: {e}"
        )
        return False


@tool(description=BROWSER_INSPECT_DESCRIPTION)
def browser_inspect(
    url: str,
    runtime: ToolRuntime,
    selectors: Optional[list[str]] = None,
    js_evaluate: str = "",
    capture_screenshot: bool = True,
    timeout_ms: int = 10000,
) -> Command:
    """Visit a URL with headless Chromium and return console logs, DOM checks, and a screenshot."""
    from playwright.sync_api import sync_playwright

    tool_call_id = runtime.tool_call_id
    console_logs: list[dict] = []
    network_failures: list[dict] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()

            page.on("console", lambda msg: console_logs.append({
                "level": msg.type,
                "text": msg.text,
            }))
            page.on("requestfailed", lambda req: network_failures.append({
                "url": req.url,
                "failure": req.failure,
            }))

            response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)

            selector_results: dict = {}
            for sel in (selectors or []):
                try:
                    selector_results[sel] = page.query_selector(sel) is not None
                except Exception:
                    selector_results[sel] = False

            js_result = None
            if js_evaluate:
                try:
                    js_result = page.evaluate(js_evaluate)
                except Exception as e:
                    js_result = f"Error: {e}"

            screenshot_b64 = None
            if capture_screenshot:
                try:
                    screenshot_b64 = base64.b64encode(page.screenshot()).decode()
                except Exception:
                    pass

            html_preview = page.content()[:3000]
            title = page.title()
            final_url = page.url
            status = response.status if response else None
            browser.close()

        diagnostic = {
            "ok": True,
            "status": status,
            "url": final_url,
            "title": title,
            "console_errors": [lg for lg in console_logs if lg["level"] in ("error", "warning")],
            "all_logs": console_logs,
            "network_failures": network_failures,
            "selectors": selector_results,
            "js_result": js_result,
            "html_preview": html_preview,
        }
        text_content = json.dumps(diagnostic, indent=2)

        # Return multimodal content when screenshot was captured.
        # StripUnsupportedMultimodalMiddleware replaces the image block with a
        # placeholder note for text-only models (DeepSeek, etc.) automatically.
        if screenshot_b64:
            content = [
                {"type": "text", "text": text_content},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
            ]
        else:
            content = text_content

    except Exception as e:
        content = json.dumps({"ok": False, "error": str(e)}, indent=2)

    return Command(
        update={
            "messages": [ToolMessage(content=content, tool_call_id=tool_call_id)]
        }
    )