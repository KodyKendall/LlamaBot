from langchain.tools import tool, ToolRuntime
from langgraph.types import Command
from langchain_core.messages import ToolMessage
from tavily import TavilyClient
import os
from bs4 import BeautifulSoup

from app.agents.leonardo.rails_agent.prompts import (
    WRITE_TODOS_DESCRIPTION,
    EDIT_DESCRIPTION,
    TOOL_DESCRIPTION,
    LIST_DIRECTORY_DESCRIPTION,
    BASH_COMMAND_FOR_RAILS_DESCRIPTION,
    SEARCH_FILE_DESCRIPTION,
)

from app.agents.leonardo.rails_agent.tool_prompts import (
    INTERNET_SEARCH_DESCRIPTION,
    GIT_STATUS_DESCRIPTION,
    GIT_COMMIT_DESCRIPTION,
    GIT_COMMAND_DESCRIPTION,
    GITHUB_CLI_DESCRIPTION,
)

from app.agents.leonardo.rails_agent.state import Todo

from pathlib import Path
import subprocess
import json
import re
import difflib

from jinja2 import Environment, FileSystemLoader


# Define base paths relative to project root
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent  # Go up to LlamaBot root
APP_DIR = PROJECT_ROOT / 'app'

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

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
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

@tool(description=SEARCH_FILE_DESCRIPTION)
def search_file(
    substring: str,
    runtime: ToolRuntime,
) -> Command:
    """Search all files in the directory for a substring."""
    tool_call_id = runtime.tool_call_id
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
WORKDIR = "/rails"  # path that contains bin/rails inside the Rails container

def get_rails_container_name():
    """Dynamically get the Rails container name by looking for containers ending with 'llamapress-1'."""
    try:
        # List all running containers using Docker API
        cmd = [
            "curl", "--silent", "--unix-socket", "/var/run/docker.sock",
            "http://localhost/containers/json"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            containers = json.loads(result.stdout)
            for container in containers:
                # Container names are in the 'Names' array with leading '/'
                names = container.get('Names', [])
                for name in names:
                    # Remove leading '/' and check if it ends with 'llamapress-1'
                    clean_name = name.lstrip('/')
                    if clean_name.endswith('llamapress-1'):
                        return clean_name
        
        # Fallback to environment-specific defaults
        # Check if we're in production (you might have an env var or other indicator)
        import os
        if os.environ.get('ENV') == 'production':
            return "llamapress-1"
        
        # Default to development container name
        return "rails-agent-llamapress-1"
        
    except Exception:
        # If anything goes wrong, return a sensible default
        return "rails-agent-llamapress-1"

# Get the container name dynamically
RAILS_CONT = get_rails_container_name()

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

@tool(description=BASH_COMMAND_FOR_RAILS_DESCRIPTION)
def bash_command(
    command: str,
    runtime: ToolRuntime,
    workdir: str = WORKDIR,
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

@tool(description="""Read the langgraph.json configuration file.
Returns the contents of /app/app/langgraph.json which registers all agents (built-in and custom).
This file maps agent names to their workflow build functions.""")
def read_langgraph_json(
    runtime: ToolRuntime,
) -> str:
    """Read the langgraph.json configuration file."""
    full_path = APP_DIR / "langgraph.json"

    if not full_path.exists():
        return "Error: langgraph.json not found at /app/app/langgraph.json"

    try:
        content = full_path.read_text()
        return f"Contents of langgraph.json:\n\n{content}"
    except Exception as e:
        return f"Error reading langgraph.json: {e}"

@tool(description="""Edit the langgraph.json configuration file to register agents.
Usage:
- old_string: The exact JSON text to find and replace
- new_string: The JSON text to replace it with
This is typically used to add new agent entries to the "graphs" object.
Example: To add a new agent, replace the graphs object with an updated version that includes your new agent.""")
def edit_langgraph_json(
    old_string: str,
    new_string: str,
    runtime: ToolRuntime,
) -> Command:
    """Edit the langgraph.json configuration file."""
    tool_call_id = runtime.tool_call_id
    full_path = APP_DIR / "langgraph.json"

    if not full_path.exists():
        error_message = "Error: langgraph.json not found at /app/app/langgraph.json"
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
        error_message = "Error: Could not find the specified text in langgraph.json"
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

    success_message = "Successfully edited langgraph.json"
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