from fastapi import APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI

# Artifact metadata
ARTIFACT_INFO = {
    "name": "project_1",
    "version": "1.0.0",
    "description": "Project 1 artifacts and assets",
    "author": "LlamaBot",
    "static_dirs": ["assets"],
    "routes_enabled": True
}

# Create a router for this project's routes
router = APIRouter()

# Function to mount static files on the main app
def mount_static_files(app: FastAPI):
    """Mount static files for this artifact"""
    try:
        app.mount("/artifacts/project_1/assets", StaticFiles(directory="artifacts/project_1/assets"), name="project_1_assets")
        return True
    except Exception as e:
        print(f"Error mounting static files for project_1: {e}")
        return False

# Optional: Function to get artifact info
def get_artifact_info():
    """Return metadata about this artifact"""
    return ARTIFACT_INFO

# Example API routes for this project
@router.get("/status")
async def get_project_status():
    """Get the status of project_1"""
    return {
        "status": "active", 
        "project": "project_1",
        "info": ARTIFACT_INFO
    }

@router.get("/files")
async def list_project_files():
    """List available files in this project"""
    import os
    from pathlib import Path
    
    assets_dir = Path("artifacts/project_1/assets")
    files = []
    
    if assets_dir.exists():
        for file in assets_dir.rglob("*"):
            if file.is_file():
                files.append({
                    "name": file.name,
                    "path": str(file.relative_to("artifacts/project_1")),
                    "size": file.stat().st_size
                })
    
    return {"files": files, "count": len(files)}