"""Saved versions of the whole app — code, database and uploaded files (LP-47).

The versions themselves live on the mothership (ZFS snapshot + pinned S3 copy);
this box never touches them. These routes forward the signed-in user's request to
the mothership's /api/leonardo/versions with the box token, and pass back what it
says — its own words for plan limits and a disabled feature, verbatim.

Saved versions never leave LlamaPress (LP-45). Whatever the mothership sends, only
the fields in the allowlists below reach the browser, and the only links that do
are https pages on the LlamaPress domain (pricing, the dashboard, a new box) —
never a storage URL or anything that downloads.
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.dependencies import get_current_user
from app.services import user_context

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])

UNREACHABLE_MESSAGE = "Couldn't reach LlamaPress to manage saved versions. Please try again in a moment."
NOT_HOSTED_MESSAGE = "Saved versions are available on apps hosted by LlamaPress."

# What a version may tell the browser about itself.
VERSION_FIELDS = {"id", "name", "created_at", "size_bytes", "status", "kind", "note"}
RESTORE_FIELDS = {"status", "version_id", "started_at", "message"}
INSTANCE_FIELDS = {"name", "status", "url", "dashboard_url"}
LIMIT_FIELDS = {"used", "max"}
TOP_FIELDS = {"enabled", "message", "error", "upgrade_url", "deleted", "can_restore", "can_launch"}
LINK_FIELDS = {"upgrade_url", "url", "dashboard_url"}

# Never a link to these, even on our own domain.
_DOWNLOAD_WORDS = re.compile(r"download|export|archive|\.tar|\.zip|\.gz|\.zst|\.sql|\.dump", re.I)
_VERSION_ID = r"[A-Za-z0-9_-]{1,64}"


def safe_link(url, mothership_url: Optional[str]) -> Optional[str]:
    """``url`` if it is an https page on the LlamaPress domain and not a download, else None."""
    if not isinstance(url, str) or not mothership_url:
        return None
    platform = (urlparse(mothership_url).hostname or "").lower()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not platform:
        return None
    if host != platform and not host.endswith("." + platform):
        return None
    if _DOWNLOAD_WORDS.search(parsed.path or "") or parsed.query:
        return None
    return url


def _pick(source, fields: set, mothership_url: Optional[str]) -> dict:
    if not isinstance(source, dict):
        return {}
    out = {}
    for key in fields & source.keys():
        value = source[key]
        if key in LINK_FIELDS:
            value = safe_link(value, mothership_url)
            if value is None:
                continue
        elif isinstance(value, (dict, list)):
            continue
        out[key] = value
    return out


def clean(body: dict, mothership_url: Optional[str]) -> dict:
    """The allowlisted shape of a mothership response."""
    out = _pick(body, TOP_FIELDS, mothership_url)
    if isinstance(body.get("versions"), list):
        out["versions"] = [_pick(v, VERSION_FIELDS, mothership_url) for v in body["versions"]]
    for key, fields in (("version", VERSION_FIELDS), ("safety_version", VERSION_FIELDS),
                        ("restore", RESTORE_FIELDS), ("instance", INSTANCE_FIELDS),
                        ("limit", LIMIT_FIELDS)):
        if isinstance(body.get(key), dict):
            out[key] = _pick(body[key], fields, mothership_url)
    return out


async def _forward(request: Request, user, method: str, path: str = "", payload: Optional[dict] = None):
    mothership = getattr(request.app.state, "mothership_client", None)
    if mothership is None or not mothership.enabled:
        if method == "GET":
            return JSONResponse({"enabled": False, "message": NOT_HOSTED_MESSAGE, "versions": []})
        return JSONResponse({"error": NOT_HOSTED_MESSAGE}, status_code=409)

    status, body = await mothership.versions_call(
        method, path, user=user_context.describe(user), payload=payload
    )
    if status is None:
        return JSONResponse({"error": UNREACHABLE_MESSAGE}, status_code=502)
    body = body or {}
    if method == "GET" and (status == 404 or 300 <= status < 400 or (
        status < 300 and "versions" not in body and "enabled" not in body
    )):
        # A mothership that hasn't shipped versions yet (it 302s to its login page):
        # no section, not an error.
        return JSONResponse({"enabled": False, "versions": []})
    if 300 <= status < 400:
        return JSONResponse({"error": UNREACHABLE_MESSAGE}, status_code=502)
    cleaned = clean(body, mothership.mothership_url)
    if status >= 400 and not cleaned.get("error"):
        cleaned["error"] = UNREACHABLE_MESSAGE if status >= 500 else "That didn't work. Please try again."
    return JSONResponse(cleaned, status_code=status)


class SaveVersionRequest(BaseModel):
    name: str


class RestoreVersionRequest(BaseModel):
    # The version's name, typed by the user in the confirmation dialog.
    confirm: str


@router.get("/api/versions")
async def list_versions(request: Request, user=Depends(get_current_user)):
    return await _forward(request, user, "GET")


@router.post("/api/versions")
async def save_version(request: Request, body: SaveVersionRequest, user=Depends(get_current_user)):
    name = body.name.strip()[:80]
    if not name:
        return JSONResponse({"error": "Give this version a name."}, status_code=422)
    return await _forward(request, user, "POST", payload={"name": name})


@router.delete("/api/versions/{version_id}")
async def delete_version(request: Request, version_id: str, user=Depends(get_current_user)):
    if not re.fullmatch(_VERSION_ID, version_id):
        return JSONResponse({"error": "Unknown version."}, status_code=422)
    return await _forward(request, user, "DELETE", f"/{version_id}")


@router.post("/api/versions/{version_id}/restore")
async def restore_version(
    request: Request, version_id: str, body: RestoreVersionRequest, user=Depends(get_current_user)
):
    if not re.fullmatch(_VERSION_ID, version_id):
        return JSONResponse({"error": "Unknown version."}, status_code=422)
    confirm = body.confirm.strip()
    if not confirm:
        return JSONResponse({"error": "Type the version's name to confirm."}, status_code=422)
    return await _forward(request, user, "POST", f"/{version_id}/restore", {"confirm": confirm})


@router.post("/api/versions/{version_id}/launch")
async def start_new_leo(request: Request, version_id: str, user=Depends(get_current_user)):
    if not re.fullmatch(_VERSION_ID, version_id):
        return JSONResponse({"error": "Unknown version."}, status_code=422)
    return await _forward(request, user, "POST", f"/{version_id}/launch")
