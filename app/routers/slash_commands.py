"""Slash Commands API for executing host scripts."""

import logging
import subprocess
import os
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies import auth, get_db_session, engineer_or_admin_required
from app.models import CommandHistory, User

logger = logging.getLogger(__name__)
router = APIRouter()

# Leonardo repo path (mounted inside LlamaBot container)
LEONARDO_PATH = os.getenv("LEONARDO_PATH", "/app/leonardo")

# Log file for slash command executions (in Leonardo repo for easy access)
SLASH_COMMAND_LOG = os.path.join(LEONARDO_PATH, "logs", "slash_commands.log")


def log_to_file(user: str, command: str, args: str, stdout: str, stderr: str, return_code: int, success: bool):
    """Log slash command execution details to Leonardo's logs directory."""
    try:
        os.makedirs(os.path.dirname(SLASH_COMMAND_LOG), exist_ok=True)
        timestamp = datetime.now().isoformat()
        status = "SUCCESS" if success else "FAILED"

        log_entry = f"""
{'='*60}
[{timestamp}] {status} - /{command}{f' {args}' if args else ''}
User: {user}
Return Code: {return_code}
{'='*60}
STDOUT:
{stdout if stdout else '(empty)'}

STDERR:
{stderr if stderr else '(empty)'}
{'='*60}

"""
        with open(SLASH_COMMAND_LOG, "a") as f:
            f.write(log_entry)
    except Exception as e:
        logger.warning(f"Could not write to slash command log: {e}")

# Command registry - maps command names to script paths and descriptions
SLASH_COMMANDS = {
    "backup": {
        "script": "bin/backups/backup_db_and_active_storage_to_s3.sh",
        "description": "Backup database and active storage to S3",
        "dangerous": True,
        "confirm_message": "This will create a full backup to S3. Continue?"
    },
    "list-backups": {
        "script": "bin/backups/list_backups.sh",
        "description": "List recent S3 backups with timestamps and sizes",
        "dangerous": False,
        "confirm_message": "View backup status?"
    },
    "restore": {
        "script": None,
        "command": "echo 'y' | bin/db/restore_s3.sh",
        "description": "Restore database from latest S3 backup",
        "dangerous": True,
        "confirm_message": "⚠️ WARNING: This will DESTROY all existing database data and restore from the latest S3 backup. This cannot be undone. Continue?"
    },
    "restore-storage": {
        "script": None,
        "command": "echo 'y' | bin/backups/restore_storage.sh",
        "description": "Restore active storage files from latest S3 backup",
        "dangerous": True,
        "confirm_message": "⚠️ WARNING: This will overwrite all storage files with the latest S3 backup. Continue?"
    },
    "install-cron": {
        "script": "bin/install/setup-cron-db-s3-backups.sh",
        "description": "Install automatic backup cron job",
        "dangerous": True,
        "confirm_message": "This will install a daily backup cron job. Continue?"
    },
    "chown": {
        "script": None,  # Direct command
        "command": "chown -R $(id -u):$(id -g) .",
        "description": "Fix file ownership permissions",
        "dangerous": True,
        "confirm_message": "This will change ownership of all files to current user. Continue?"
    },
    "truncate-checkpoints": {
        "script": "bin/db/truncate_checkpoints.sh",
        "description": "Truncate LangGraph checkpoint tables",
        "dangerous": True,
        "confirm_message": "This will DELETE all conversation history. This cannot be undone. Continue?"
    },
    "restart": {
        "script": "bin/restart",
        "description": "Restart llamapress and llamabot containers",
        "dangerous": True,
        "confirm_message": "This will restart the application. You may lose your connection briefly. Continue?"
    },
    "bash": {
        "script": None,
        "command": None,  # Uses args from request
        "description": "Run a custom bash command",
        "dangerous": True,
        "confirm_message": "This will execute a custom bash command. Continue?",
        "accepts_args": True
    },
    "history": {
        "script": None,
        "command": None,
        "description": "View command execution history",
        "dangerous": False,
        "confirm_message": "View command history?",
        "client_only": True  # Handled entirely on frontend
    },
    "gh": {
        "script": None,
        "command": "timeout 3 gh auth login -p https -h github.com -w 2>&1 || true",
        "description": "Authenticate with GitHub (opens browser)",
        "dangerous": False,
        "confirm_message": "This will start GitHub authentication. A browser tab will open and the code will be copied to your clipboard. Continue?",
        "special_handler": "gh_auth"  # Frontend handles code copy + URL open
    },
    "setup-ssh": {
        "script": "bin/install/setup-ssh-full.sh",
        "description": "Setup SSH key for VSCode container to access host",
        "dangerous": False,
        "confirm_message": "This will generate an SSH key and configure VSCode container access to the host. Continue?"
    }
}


class ExecuteRequest(BaseModel):
    command: str
    args: Optional[str] = None  # For /bash command or other commands that accept arguments


# Host path where Leonardo lives (for host commands via nsenter on Linux)
HOST_LEONARDO_PATH = os.getenv("HOST_LEONARDO_PATH", "/home/ubuntu/Leonardo")


def is_native_linux_host() -> bool:
    """
    Detect if we're running on native Linux (where nsenter will work)
    vs Docker Desktop on macOS/Windows (where it won't).
    """
    # Check if /bin/bash exists in PID 1's namespace by trying nsenter
    # On native Linux with pid:host, this will work
    # On Docker Desktop, PID 1 is the VM's init, not the host's
    try:
        result = subprocess.run(
            ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "/bin/bash", "-c", "echo ok"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0 and "ok" in result.stdout
    except Exception:
        return False


def execute_command(command: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """
    Execute a command - uses nsenter on native Linux, direct execution otherwise.
    On macOS (Docker Desktop), commands run inside the container with mounted Leonardo.
    On Linux (production), commands run on the host via nsenter.
    """
    if is_native_linux_host():
        # Linux production: use nsenter to run on host
        nsenter_cmd = [
            "nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p",
            "--", "/bin/bash", "-c", f"cd {HOST_LEONARDO_PATH} && {command}"
        ]
        return subprocess.run(
            nsenter_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
    else:
        # macOS dev (or any non-Linux): run directly in container
        # Leonardo is mounted at LEONARDO_PATH (/app/leonardo)
        return subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=LEONARDO_PATH
        )


def execute_host_command(cmd_config: dict, args: Optional[str] = None) -> dict:
    """
    Execute a command on the HOST machine via nsenter.
    Scripts run from the Leonardo directory on the host.
    """
    try:
        if cmd_config.get("script"):
            # Execute a script file on host
            script_path = cmd_config["script"]  # Relative to Leonardo

            # Check if script exists in mounted path (for validation)
            local_script_path = os.path.join(LEONARDO_PATH, script_path)
            if not os.path.exists(local_script_path):
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": f"Script not found: {script_path}",
                    "return_code": 1
                }

            # Execute the script
            result = execute_command(f"bash {script_path}", timeout=300)

        elif cmd_config.get("accepts_args") and args:
            # Execute custom bash command
            result = execute_command(args, timeout=120)

        elif cmd_config.get("command"):
            # Execute a predefined direct command
            command = cmd_config["command"]
            result = execute_command(command, timeout=60)

        else:
            return {
                "success": False,
                "stdout": "",
                "stderr": "No script, command, or args provided",
                "return_code": 1
            }

        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip() if result.stdout else "",
            "stderr": result.stderr.strip() if result.stderr else "",
            "return_code": result.returncode
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": "Command timed out after maximum allowed time",
            "return_code": -1
        }
    except Exception as e:
        logger.exception(f"Error executing command: {e}")
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Error: {str(e)}",
            "return_code": -1
        }


@router.get("/api/slash-commands", response_class=JSONResponse)
async def get_slash_commands(current_user: User = Depends(engineer_or_admin_required)) -> List[dict]:
    """Get list of available slash commands."""
    return [
        {
            "name": name,
            "description": cmd["description"],
            "dangerous": cmd.get("dangerous", False),
            "confirm_message": cmd.get("confirm_message", "Are you sure?")
        }
        for name, cmd in SLASH_COMMANDS.items()
    ]


@router.post("/api/slash-commands/execute", response_class=JSONResponse)
async def execute_slash_command(
    request: ExecuteRequest,
    current_user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Execute a slash command."""
    cmd_name = request.command
    args = request.args

    if cmd_name not in SLASH_COMMANDS:
        raise HTTPException(status_code=404, detail=f"Unknown command: {cmd_name}")

    cmd_config = SLASH_COMMANDS[cmd_name]

    # Client-only commands (like /history) shouldn't be executed server-side
    if cmd_config.get("client_only"):
        return {
            "success": True,
            "output": "This command is handled by the client",
            "client_only": True,
            "command": cmd_name
        }

    # Validate /bash command has args
    if cmd_config.get("accepts_args") and not args:
        raise HTTPException(status_code=400, detail=f"Command /{cmd_name} requires arguments")

    logger.info(f"User '{current_user.username}' executing slash command: /{cmd_name}{f' {args}' if args else ''}")

    # Execute the command
    result = execute_host_command(cmd_config, args=args)

    # Log to file in Leonardo repo
    log_to_file(
        user=current_user.username,
        command=cmd_name,
        args=args or "",
        stdout=result.get("stdout", ""),
        stderr=result.get("stderr", ""),
        return_code=result.get("return_code", -1),
        success=result["success"]
    )

    # Save to database
    history_entry = CommandHistory(
        command=cmd_name,
        args=args,
        username=current_user.username,
        success=result["success"],
        stdout=result.get("stdout", ""),
        stderr=result.get("stderr", ""),
        return_code=result.get("return_code", -1)
    )
    session.add(history_entry)
    session.commit()

    if result["success"]:
        logger.info(f"Slash command /{cmd_name} completed successfully")
    else:
        logger.warning(f"Slash command /{cmd_name} failed (rc={result.get('return_code')}): {result.get('stderr', 'Unknown error')}")

    # Build output for display (combine stdout and stderr)
    output = result.get("stdout", "")
    if result.get("stderr"):
        output += f"\n\nSTDERR:\n{result['stderr']}" if output else result["stderr"]

    return {
        "success": result["success"],
        "output": output if output else "Command completed with no output",
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "return_code": result.get("return_code", -1),
        "command": cmd_name,
        "args": args,
        "special_handler": cmd_config.get("special_handler")
    }


@router.get("/api/slash-commands/history", response_class=JSONResponse)
async def get_command_history(
    current_user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Get the last 50 command executions."""
    stmt = (
        select(CommandHistory)
        .order_by(CommandHistory.executed_at.desc())
        .limit(50)
    )
    history = session.exec(stmt).all()

    return [
        {
            "id": entry.id,
            "command": entry.command,
            "args": entry.args,
            "username": entry.username,
            "success": entry.success,
            "stdout": entry.stdout,
            "stderr": entry.stderr,
            "return_code": entry.return_code,
            "executed_at": entry.executed_at.isoformat()
        }
        for entry in history
    ]
