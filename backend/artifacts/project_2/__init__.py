from fastapi import APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI

# Artifact metadata
ARTIFACT_INFO = {
    "name": "project_2",
    "version": "2.1.0",
    "description": "Second project with different features",
    "author": "LlamaBot Team",
    "static_dirs": ["assets", "docs"],
    "routes_enabled": True
}

# Create a router for this project's routes
router = APIRouter()

# Function to mount static files on the main app
def mount_static_files(app: FastAPI):
    """Mount static files for this artifact"""
    try:
        app.mount("/artifacts/project_2/assets", StaticFiles(directory="artifacts/project_2/assets"), name="project_2_assets")
        return True
    except Exception as e:
        print(f"Error mounting static files for project_2: {e}")
        return False

# Optional: Function to get artifact info
def get_artifact_info():
    """Return metadata about this artifact"""
    return ARTIFACT_INFO

# Example API routes for this project
@router.get("/status")
async def get_project_status():
    """Get the status of project_2"""
    return {
        "status": "experimental", 
        "project": "project_2",
        "info": ARTIFACT_INFO,
        "features": ["dynamic-loading", "auto-discovery", "modular-architecture"]
    }

@router.get("/config")
async def get_project_config():
    """Get configuration for project_2"""
    return {
        "project": "project_2",
        "config": {
            "debug": True,
            "version": ARTIFACT_INFO["version"],
            "endpoints": ["/status", "/config", "/metrics"]
        }
    }

@router.get("/metrics")
async def get_project_metrics():
    """Get metrics for project_2"""
    import time
    return {
        "project": "project_2",
        "timestamp": time.time(),
        "uptime": "dynamic",
        "memory_usage": "low",
        "requests_served": "auto-tracked"
    } 