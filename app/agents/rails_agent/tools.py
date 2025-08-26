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
    BASH_COMMAND_FOR_RAILS_DESCRIPTION,
    GIT_STATUS_DESCRIPTION,
    GIT_COMMIT_DESCRIPTION,
    GIT_COMMAND_DESCRIPTION,
    SEARCH_FILE_DESCRIPTION,
    VIEW_CURRENT_PAGE_HTML_DESCRIPTION,
    GITHUB_CLI_DESCRIPTION,
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
GIT_HTML_OUTPUT_PATH = APP_DIR / 'agents' / 'rails_agent' / 'page.html'

@tool(description=WRITE_TODOS_DESCRIPTION)
def write_todos(
    todos: list[Todo], tool_call_id: Annotated[str, InjectedToolCallId]
) -> Command:
    
    env = Environment(loader=FileSystemLoader(APP_DIR / 'agents' / 'rails_agent' / 'templates'))
    template = env.get_template("todo.html.j2")

    html = template.render(todos=todos)

    with open(HTML_OUTPUT_PATH, "w", encoding='utf-8') as f:
        f.write(html)

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
    state: Annotated[RailsAgentState, InjectedState],
    offset: int = 0,
    limit: int = 2000,
) -> str:
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
        git_status(tool_call_id) # hacky - this will update the git status page so the user can see the changes.
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

    git_status(tool_call_id) # hacky - this will update the git status page so the user can see the changes.

    return Command(
        update={
            "messages": [ToolMessage(result_msg, tool_call_id=tool_call_id)],
        }
    )

@tool(description=SEARCH_FILE_DESCRIPTION)
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
    tool_call_id: Annotated[str, InjectedToolCallId],
    workdir: str = WORKDIR
) -> Command:
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
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    
    """Get the status of the git repository."""
    def run_git(cmd: str) -> str:
        result = subprocess.run(
            ["/bin/sh", "-lc", f"git -C /app/app/rails {cmd}"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git command failed: {cmd}\n{result.stderr}")
        return result.stdout.strip()
    
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
    status = run_git("status --porcelain=v2 --branch")
    log = run_git(
        "log -n 10 --pretty=format:'{\"hash\":\"%H\",\"author\":\"%an\",\"date\":\"%ad\",\"subject\":\"%s\"},'"
    )
    
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
            try:
                diff_output = run_git(f"diff HEAD -- '{new_path}'")
                if not diff_output:
                    # If no diff with HEAD, try staged diff
                    diff_output = run_git(f"diff --cached -- '{new_path}'")
            except:
                diff_output = f"Could not get diff for renamed file: {file_path}"
        else:
            try:
                # Try to get diff for the file
                diff_output = run_git(f"diff HEAD -- '{file_path}'")
                if not diff_output:
                    # If no diff with HEAD, try staged diff
                    diff_output = run_git(f"diff --cached -- '{file_path}'")
                if not diff_output and file_info['status_code'].endswith('M'):
                    # For worktree modifications, try diff without HEAD
                    diff_output = run_git(f"diff -- '{file_path}'")
            except:
                diff_output = f"Could not get diff for file: {file_path}"
        
        file_info['diff'] = diff_output if diff_output else f"No changes to display for {file_path}"

    # Parse log into JSON
    log_json = "[" + log.strip().rstrip(",") + "]"
    commits = json.loads(log_json) if log_json.strip("[]") else []
    
    # Pre-fetch commit diffs for all commits
    commit_diffs = {}
    for commit in commits:
        commit_hash = commit['hash']
        try:
            # Get commit diff
            diff_output = run_git(f"show --no-merges {commit_hash}")
            
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

    # Render with Jinja
    env = Environment(loader=FileSystemLoader(APP_DIR / 'agents' / 'rails_agent' / 'templates'))
    template = env.get_template("git-status.html.j2")

    html = template.render(
        status=status.splitlines(),
        commits=commits,
        changed_files=changed_files,
        format_diff=format_diff,
        commit_mode=False,
        selected_commit=None,
        commit_diffs=commit_diffs
    )

    with open(GIT_HTML_OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

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
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Commit the changes to the git repository."""
    # First add all changes
    add_result = subprocess.run(["/bin/sh", "-lc", "git -C /app/app/rails add ."], capture_output=True, text=True, timeout=30)
    
    # Then commit the changes - use subprocess list format to avoid shell escaping issues
    commit_result = subprocess.run(["git", "-C", "/app/app/rails", "commit", "-m", message], capture_output=True, text=True, timeout=30)
    
    # Combine the output from both commands
    output = f"Git add:\n{add_result.stdout}"
    if add_result.stderr:
        output += f"\nGit add errors:\n{add_result.stderr}"
    
    output += f"\n\nGit commit:\n{commit_result.stdout}"
    if commit_result.stderr:
        output += f"\nGit commit errors:\n{commit_result.stderr}"
    
    # After committing, automatically run git status to show the current state
    try:
        status_command = git_status(tool_call_id)
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
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Configure the git repository."""
    git_result = subprocess.run(["/bin/sh", "-lc", f"git -C /app/app/rails {command}"], capture_output=True, text=True, timeout=30)

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
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Use the github cli to do anything else related specifically to github."""

    github_result = subprocess.run(["/bin/sh", "-lc", f"gh {command}"], capture_output=True, text=True, timeout=30)

    output = f"Github result:\n{github_result.stdout}"
    if github_result.stderr:
        output += f"\nGithub command errors:\n{github_result.stderr}"

    return Command(
        update={
            "messages": [ToolMessage(output, tool_call_id=tool_call_id)],
        }
    )

@tool(description=VIEW_CURRENT_PAGE_HTML_DESCRIPTION)
def view_page(state: Annotated[RailsAgentState, InjectedState], tool_call_id: Annotated[str, InjectedToolCallId]):
    debug_info = state.get("debug_info")
    return Command(
        update={
            "messages": [ToolMessage(debug_info, tool_call_id=tool_call_id)],
    }
    )