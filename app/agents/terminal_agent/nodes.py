from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from dotenv import load_dotenv
from functools import partial
import os
import subprocess
import tempfile
from typing import Optional, Dict, List
from pathlib import Path
from pydantic import Field

load_dotenv()

from langgraph.graph import MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from langgraph.graph import START, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.prebuilt import ToolNode, InjectedState

from typing import Annotated
import json

# Custom state class for the terminal agent
class TerminalAgentState(MessagesState): 
    agent_prompt: Optional[str] = None
    current_directory: Optional[str] = None
    last_command_output: Optional[str] = None
    command_history: List[Dict] = Field(default_factory=list)
    session_context: Optional[str] = None
    original_request: Optional[str] = None

# Tools
@tool
def read_file(file_path: str, state: Annotated[dict, InjectedState]) -> str:
    """Read the contents of a file."""
    try:
        # Handle relative paths by using current_directory if available
        if not os.path.isabs(file_path) and state.get("current_directory"):
            file_path = os.path.join(state.get("current_directory"), file_path)
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Limit output size to prevent overwhelming the context
        if len(content) > 10000:
            content = content[:10000] + "\n... (file truncated for brevity)"
        
        return f"Successfully read file: {file_path}\n\nContent:\n{content}"
    
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except PermissionError:
        return f"Error: Permission denied reading file: {file_path}"
    except Exception as e:
        return f"Error reading file {file_path}: {str(e)}"

@tool
def write_file(file_path: str, content: str, state: Annotated[dict, InjectedState]) -> str:
    """Write content to a file."""
    try:
        # Handle relative paths by using current_directory if available
        if not os.path.isabs(file_path) and state.get("current_directory"):
            file_path = os.path.join(state.get("current_directory"), file_path)
        
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return f"Successfully wrote {len(content)} characters to file: {file_path}"
    
    except PermissionError:
        return f"Error: Permission denied writing to file: {file_path}"
    except Exception as e:
        return f"Error writing to file {file_path}: {str(e)}"

@tool
def list_directory(state: Annotated[dict, InjectedState], path: str = "") -> str:
    """List the contents of a directory with detailed information."""
    try:
        if not path:
            path = state.get("current_directory", ".")
        
        if not os.path.isabs(path) and state.get("current_directory"):
            path = os.path.join(state.get("current_directory"), path)
        
        if not os.path.exists(path):
            return f"Error: Path does not exist: {path}"
        
        if not os.path.isdir(path):
            return f"Error: Path is not a directory: {path}"
        
        items = []
        for item in sorted(os.listdir(path)):
            item_path = os.path.join(path, item)
            try:
                stat = os.stat(item_path)
                if os.path.isdir(item_path):
                    items.append(f"📁 {item}/")
                else:
                    size = stat.st_size
                    if size < 1024:
                        size_str = f"{size}B"
                    elif size < 1024 * 1024:
                        size_str = f"{size // 1024}KB"
                    else:
                        size_str = f"{size // (1024 * 1024)}MB"
                    items.append(f"📄 {item} ({size_str})")
            except (OSError, PermissionError):
                items.append(f"❓ {item} (permission denied)")
        
        return f"Contents of {path}:\n" + "\n".join(items)
    
    except PermissionError:
        return f"Error: Permission denied accessing directory: {path}"
    except Exception as e:
        return f"Error listing directory {path}: {str(e)}"

@tool
def run_command(command: str, state: Annotated[dict, InjectedState], working_dir: str = "") -> str:
    """Execute a shell command safely and return the output."""
    try:
        # Determine working directory
        if not working_dir:
            working_dir = state.get("current_directory", os.getcwd())
        
        # Security: Block dangerous commands
        dangerous_patterns = [
            'rm -rf /', 'sudo rm', 'rm -rf *', 'format', 'fdisk', 
            'dd if=', 'mkfs', '> /dev/', 'chmod 777', 'chown -R'
        ]
        
        command_lower = command.lower()
        for pattern in dangerous_patterns:
            if pattern in command_lower:
                return f"Error: Command blocked for security reasons: {command}"
        
        # Execute the command
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=working_dir
        )
        
        output_parts = []
        if result.stdout:
            output_parts.append(f"Output:\n{result.stdout}")
        if result.stderr:
            output_parts.append(f"Error:\n{result.stderr}")
        
        output = "\n".join(output_parts) if output_parts else "Command completed with no output"
        
        # Update current directory if command was 'cd'
        if command.strip().startswith('cd '):
            try:
                new_dir = os.path.abspath(os.path.join(working_dir, command.strip()[3:].strip()))
                if os.path.isdir(new_dir):
                    # Note: We can't actually update the state here since it's injected read-only
                    # The agent will need to track directory changes
                    output += f"\nDirectory changed to: {new_dir}"
            except:
                pass
        
        return f"Command: {command}\nExit code: {result.returncode}\n{output}"
    
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after 30 seconds: {command}"
    except Exception as e:
        return f"Error executing command '{command}': {str(e)}"

@tool
def get_current_directory(state: Annotated[dict, InjectedState]) -> str:
    """Get the current working directory."""
    current_dir = state.get("current_directory", os.getcwd())
    return f"Current directory: {current_dir}"

@tool
def search_files(pattern: str, state: Annotated[dict, InjectedState], directory: str = "") -> str:
    """Search for files matching a pattern in the given directory."""
    try:
        if not directory:
            directory = state.get("current_directory", ".")
        
        import glob
        search_path = os.path.join(directory, "**", pattern)
        matches = glob.glob(search_path, recursive=True)
        
        if not matches:
            return f"No files found matching pattern '{pattern}' in {directory}"
        
        # Limit results to prevent overwhelming output
        if len(matches) > 50:
            matches = matches[:50]
            truncated = True
        else:
            truncated = False
        
        result = f"Found {len(matches)} files matching '{pattern}' in {directory}:\n"
        for match in matches:
            rel_path = os.path.relpath(match, directory)
            result += f"  {rel_path}\n"
        
        if truncated:
            result += "  ... (results truncated, showing first 50 matches)"
        
        return result
    
    except Exception as e:
        return f"Error searching for files: {str(e)}"

# Global tools list
tools = [read_file, write_file, list_directory, run_command, get_current_directory, search_files]

# Node
def terminal_agent(state: TerminalAgentState):
    additional_instructions = state.get("agent_prompt", "")
    current_dir = state.get("current_directory", os.getcwd())
    last_output = state.get("last_command_output", "")
    command_history = state.get("command_history", [])
    original_request = state.get("original_request", "")
    
    # Build context from command history
    history_context = ""
    if command_history:
        recent_commands = command_history[-3:]  # Last 3 commands
        history_context = "\n\nRecent command history:\n"
        for i, cmd in enumerate(recent_commands, 1):
            history_context += f"{i}. {cmd.get('command', 'Unknown')}\n   Result: {cmd.get('output', 'No output')[:200]}...\n"
    
    # System message
    sys_msg = SystemMessage(content=f"""You are a Terminal Agent, an advanced AI assistant that can help with file system operations, command execution, and terminal tasks.

Current Context:
- Current directory: {current_dir}
- Original request: {original_request}
- Last command output: {last_output[:500] if last_output else 'None'}
{history_context}

Available Tools:
- read_file: Read contents of files
- write_file: Write content to files  
- list_directory: List directory contents with detailed info
- run_command: Execute shell commands safely
- get_current_directory: Get current working directory
- search_files: Search for files matching patterns

You can maintain a persistent terminal session by:
1. Using run_command to execute shell commands
2. Reading and writing files as needed
3. Building up context over multiple interactions
4. Tracking directory changes and command history

Safety Guidelines:
- Commands are executed with safety checks
- Dangerous operations are blocked
- File operations handle permissions gracefully
- Commands timeout after 30 seconds

When helping users:
1. Break complex tasks into steps
2. Use appropriate tools for each operation
3. Provide clear explanations of what you're doing
4. Handle errors gracefully and suggest alternatives
5. Maintain context across multiple commands

Additional instructions: {additional_instructions}

Always explain what you're doing and why, and ask for clarification if the user's request is ambiguous.""")

    llm = ChatOpenAI(model="o4-mini")
    llm_with_tools = llm.bind_tools(tools)
    return {"messages": [llm_with_tools.invoke([sys_msg] + state["messages"])]}

def build_workflow(checkpointer=None):
    # Graph
    builder = StateGraph(TerminalAgentState)

    # Define nodes: these do the work
    builder.add_node("terminal_agent", terminal_agent)
    builder.add_node("tools", ToolNode(tools))

    # Define edges: these determine how the control flow moves
    builder.add_edge(START, "terminal_agent")
    builder.add_conditional_edges(
        "terminal_agent",
        # If the latest message (result) from terminal_agent is a tool call -> tools_condition routes to tools
        # If the latest message (result) from terminal_agent is a not a tool call -> tools_condition routes to END
        tools_condition,
    )
    builder.add_edge("tools", "terminal_agent")

    react_graph = builder.compile(checkpointer=checkpointer)

    return react_graph