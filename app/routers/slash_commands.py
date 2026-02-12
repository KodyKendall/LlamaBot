"""Slash Commands API for executing host scripts."""

import json
import logging
import subprocess
import os
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.dependencies import auth

logger = logging.getLogger(__name__)
router = APIRouter()

# Leonardo repo path (mounted inside LlamaBot container)
LEONARDO_PATH = os.getenv("LEONARDO_PATH", "/app/leonardo")

# Command registry - maps command names to script paths and descriptions
SLASH_COMMANDS = {
    "backup": {
        "script": "bin/backups/backup_db_and_active_storage_to_s3.sh",
        "description": "Backup database and active storage to S3",
        "dangerous": True,
        "confirm_message": "This will create a full backup to S3. Continue?"
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
    }
}


class ExecuteRequest(BaseModel):
    command: str


def execute_host_command(cmd_config: dict) -> dict:
    """
    Execute a command from the mounted Leonardo directory.
    Scripts have access to Docker socket for container operations.
    """
    try:
        if cmd_config.get("script"):
            # Execute a script file
            script_path = os.path.join(LEONARDO_PATH, cmd_config["script"])

            # Check if script exists
            if not os.path.exists(script_path):
                return {
                    "success": False,
                    "output": f"Script not found: {script_path}"
                }

            # Execute the script
            result = subprocess.run(
                ["bash", script_path],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
                cwd=LEONARDO_PATH
            )
        else:
            # Execute a direct command
            command = cmd_config["command"]
            result = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=60,  # 1 minute timeout for direct commands
                cwd=LEONARDO_PATH
            )

        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr if output else result.stderr

        return {
            "success": result.returncode == 0,
            "output": output.strip() if output else "Command completed with no output",
            "return_code": result.returncode
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "output": "Command timed out after maximum allowed time"
        }
    except Exception as e:
        logger.exception(f"Error executing command: {e}")
        return {
            "success": False,
            "output": f"Error: {str(e)}"
        }


@router.get("/api/slash-commands", response_class=JSONResponse)
async def get_slash_commands(username: str = Depends(auth)) -> List[dict]:
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
    username: str = Depends(auth)
):
    """Execute a slash command."""
    cmd_name = request.command

    if cmd_name not in SLASH_COMMANDS:
        raise HTTPException(status_code=404, detail=f"Unknown command: {cmd_name}")

    cmd_config = SLASH_COMMANDS[cmd_name]

    logger.info(f"User '{username}' executing slash command: /{cmd_name}")

    # Execute the command
    result = execute_host_command(cmd_config)

    if result["success"]:
        logger.info(f"Slash command /{cmd_name} completed successfully")
    else:
        logger.warning(f"Slash command /{cmd_name} failed: {result.get('output', 'Unknown error')}")

    return {
        "success": result["success"],
        "output": result["output"],
        "command": cmd_name
    }
