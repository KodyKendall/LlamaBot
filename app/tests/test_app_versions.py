"""LP-47: whole-app saved versions — the box half.

The box is a thin, signed-in proxy to the mothership's /api/leonardo/versions.
What these tests pin down:

* every action reaches the right mothership path, with the signed-in user attached;
* restore carries the typed confirmation, and refuses without it;
* the mothership's own message and upgrade link reach the browser verbatim
  (plan limit, feature off);
* nothing that could download a version ever reaches the browser (LP-45) — only an
  allowlist of fields passes, and only https links on the LlamaPress domain;
* the routes sit behind the role boundary like the other workspace-changing routes;
* Leo points "make me a backup" at Save version.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_current_user
from app.routers import versions as versions_router
from app.services.mothership_client import MothershipClient


SIGNED_IN = SimpleNamespace(
    id=7, username="ana", email="ana@example.com", llamapress_user_guid="guid-ana",
    role="engineer", is_admin=False,
)

SAVED = {
    "id": "v_1", "name": "before invoices", "created_at": "2026-11-10T14:00:00Z",
    "size_bytes": 52_428_800, "status": "saved", "kind": "user",
}


class StubMothership:
    """Records calls to versions_call and answers with a canned (status, body)."""

    enabled = True
    mothership_url = "https://llamapress.ai"

    def __init__(self, answer=(200, {"enabled": True, "versions": [SAVED]})):
        self.answer = answer
        self.calls = []

    async def versions_call(self, method, path="", *, user=None, payload=None):
        self.calls.append({"method": method, "path": path, "user": user, "payload": payload})
        return self.answer


def make_client(stub):
    app = FastAPI()
    app.include_router(versions_router.router)
    app.state.mothership_client = stub
    app.dependency_overrides[get_current_user] = lambda: SIGNED_IN
    return TestClient(app)


# --------------------------------------------------------------- routing to the mothership

def test_list_proxies_to_the_mothership_with_the_signed_in_user():
    stub = StubMothership()
    r = make_client(stub).get("/api/versions")

    assert r.status_code == 200
    assert r.json()["versions"][0]["name"] == "before invoices"
    call = stub.calls[0]
    assert (call["method"], call["path"]) == ("GET", "")
    assert call["user"]["llamapress_user_guid"] == "guid-ana"


def test_save_sends_the_name():
    stub = StubMothership((201, {"version": {**SAVED, "status": "saving"}}))
    r = make_client(stub).post("/api/versions", json={"name": "  before invoices  "})

    assert r.status_code == 201
    assert r.json()["version"]["status"] == "saving"
    assert stub.calls[0]["method"] == "POST"
    assert stub.calls[0]["payload"] == {"name": "before invoices"}


def test_save_without_a_name_is_refused_on_the_box():
    stub = StubMothership()
    r = make_client(stub).post("/api/versions", json={"name": "   "})
    assert r.status_code == 422
    assert stub.calls == []


def test_delete_proxies():
    stub = StubMothership((200, {"deleted": True}))
    r = make_client(stub).delete("/api/versions/v_1")
    assert r.status_code == 200
    assert (stub.calls[0]["method"], stub.calls[0]["path"]) == ("DELETE", "/v_1")


def test_restore_carries_the_typed_confirmation():
    stub = StubMothership((202, {"restore": {"status": "restoring", "version_id": "v_1"},
                                 "safety_version": {**SAVED, "id": "v_2", "kind": "safety"}}))
    r = make_client(stub).post("/api/versions/v_1/restore", json={"confirm": "before invoices"})

    assert r.status_code == 202
    assert r.json()["restore"]["status"] == "restoring"
    assert r.json()["safety_version"]["kind"] == "safety"
    assert (stub.calls[0]["method"], stub.calls[0]["path"]) == ("POST", "/v_1/restore")
    assert stub.calls[0]["payload"] == {"confirm": "before invoices"}


def test_restore_without_confirmation_never_reaches_the_mothership():
    stub = StubMothership()
    r = make_client(stub).post("/api/versions/v_1/restore", json={"confirm": ""})
    assert r.status_code == 422
    assert stub.calls == []


def test_start_new_leo_proxies_and_returns_the_new_box():
    stub = StubMothership((202, {"instance": {
        "name": "ana-copy", "status": "provisioning",
        "dashboard_url": "https://llamapress.ai/dashboard",
        "url": "https://ana-copy.llamapress.ai",
    }}))
    r = make_client(stub).post("/api/versions/v_1/launch", json={})

    assert r.status_code == 202
    assert r.json()["instance"]["dashboard_url"] == "https://llamapress.ai/dashboard"
    assert r.json()["instance"]["url"] == "https://ana-copy.llamapress.ai"
    assert (stub.calls[0]["method"], stub.calls[0]["path"]) == ("POST", "/v_1/launch")


@pytest.mark.parametrize("bad_id", ["..", "v_1/../../admin", "a b", "x" * 200])
def test_version_ids_cannot_steer_the_mothership_path(bad_id):
    stub = StubMothership()
    r = make_client(stub).delete(f"/api/versions/{bad_id}")
    assert r.status_code in (404, 422)
    assert stub.calls == []


# --------------------------------------------------------------- server messages verbatim

def test_plan_limit_message_and_pricing_link_pass_through_verbatim():
    message = "You've used all 1 saved versions on the Free plan."
    stub = StubMothership((402, {"error": message, "upgrade_url": "https://llamapress.ai/pricing"}))
    r = make_client(stub).post("/api/versions", json={"name": "v2"})

    assert r.status_code == 402
    assert r.json() == {"error": message, "upgrade_url": "https://llamapress.ai/pricing"}


def test_feature_disabled_is_passed_through():
    stub = StubMothership((200, {"enabled": False, "message": "Saved versions are coming soon."}))
    body = make_client(stub).get("/api/versions").json()
    assert body["enabled"] is False
    assert body["message"] == "Saved versions are coming soon."


def test_box_without_a_mothership_says_so_and_offers_nothing():
    stub = StubMothership()
    stub.enabled = False
    body = make_client(stub).get("/api/versions").json()
    assert body["enabled"] is False
    assert body["versions"] == []
    assert stub.calls == []


def test_mothership_unreachable_is_a_plain_message_not_a_500():
    stub = StubMothership((None, None))
    r = make_client(stub).get("/api/versions")
    assert r.status_code == 502
    assert "try again" in r.json()["error"].lower()


# --------------------------------------------------------------- no way to download (LP-45)

LEAKY = {
    "enabled": True,
    "download_url": "https://bucket.s3.amazonaws.com/x.tar?X-Amz-Signature=abc",
    "upgrade_url": "https://evil.example.com/pricing",
    "versions": [{
        **SAVED,
        "s3_key": "backups/ana/v_1.tar.zst",
        "presigned_url": "https://bucket.s3.amazonaws.com/v_1?X-Amz-Signature=abc",
        "archive_url": "https://llamapress.ai/archive/v_1.tar",
        "zfs_snapshot": "tank/ana@v_1",
    }],
    "restore": {"status": "idle", "url": "https://bucket.s3.amazonaws.com/r"},
}


def test_only_allowlisted_fields_reach_the_browser():
    stub = StubMothership((200, LEAKY))
    body = make_client(stub).get("/api/versions").json()
    raw = json.dumps(body)

    assert set(body["versions"][0]) <= versions_router.VERSION_FIELDS
    for needle in ("amazonaws", "X-Amz", "s3_key", "presigned", "archive", "download", "tank/", "evil.example"):
        assert needle not in raw, needle
    assert body["versions"][0]["name"] == "before invoices"


def test_error_bodies_are_filtered_too():
    stub = StubMothership((402, {"error": "Limit reached.", "upgrade_url": "https://llamapress.ai/pricing",
                                 "download_url": "https://bucket.s3.amazonaws.com/x"}))
    body = make_client(stub).post("/api/versions", json={"name": "v"}).json()
    assert "download_url" not in body


@pytest.mark.parametrize("url,kept", [
    ("https://llamapress.ai/pricing", True),
    ("https://ana-copy.llamapress.ai", True),
    ("http://llamapress.ai/pricing", False),
    ("https://llamapress.ai.evil.com/pricing", False),
    ("https://bucket.s3.amazonaws.com/x", False),
    ("javascript:alert(1)", False),
    ("https://llamapress.ai/versions/v_1/download", False),
])
def test_links_are_https_on_the_platform_domain_and_never_a_download(url, kept):
    assert (versions_router.safe_link(url, "https://llamapress.ai") == url) is kept


def test_no_download_route_exists():
    paths = [getattr(r, "path", "") for r in versions_router.router.routes]
    assert paths and not any(word in p for p in paths for word in ("download", "export", "archive"))


# --------------------------------------------------------------- the mothership client

@pytest.mark.asyncio
async def test_versions_call_uses_box_auth_and_names_the_instance(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(402, json={"error": "Limit reached."})

    client = MothershipClient.__new__(MothershipClient)
    client.config = {"mothership_url": "https://llamapress.ai", "mothership_api_token": "tok",
                     "instance_name": "ana"}
    status, body = await client.versions_call(
        "POST", "/v_1/restore", user={"id": 7}, payload={"confirm": "x"},
        transport=httpx.MockTransport(handler),
    )

    assert (status, body) == (402, {"error": "Limit reached."})
    assert seen["url"] == "https://llamapress.ai/api/leonardo/versions/v_1/restore"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"instance_name": "ana", "user": {"id": 7}, "confirm": "x"}


@pytest.mark.asyncio
async def test_versions_list_sends_the_instance_as_a_query_param():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"versions": []})

    client = MothershipClient.__new__(MothershipClient)
    client.config = {"mothership_url": "https://llamapress.ai", "mothership_api_token": "tok",
                     "instance_name": "ana"}
    await client.versions_call("GET", "", user=None, transport=httpx.MockTransport(handler))
    assert seen["url"] == "https://llamapress.ai/api/leonardo/versions?instance_name=ana"


@pytest.mark.asyncio
async def test_versions_call_unreachable_returns_none_pair():
    def handler(request):
        raise httpx.ConnectError("down")

    client = MothershipClient.__new__(MothershipClient)
    client.config = {"mothership_url": "https://llamapress.ai", "mothership_api_token": "tok",
                     "instance_name": "ana"}
    assert await client.versions_call("GET", transport=httpx.MockTransport(handler)) == (None, None)


# --------------------------------------------------------------- role boundary

def test_versions_routes_are_system_routes():
    from app.permissions import route_allowed

    security_only = SimpleNamespace(is_admin=False, is_security_admin=True, is_system_admin=False)
    standard = SimpleNamespace(is_admin=False, is_security_admin=False, is_system_admin=False)
    sysadmin = SimpleNamespace(is_admin=False, is_security_admin=False, is_system_admin=True)

    # A security-only admin can look but never restore.
    assert route_allowed(security_only, "GET", "/api/versions", False)
    assert not route_allowed(security_only, "POST", "/api/versions/v_1/restore", False)
    # Under ZDR, only a system admin changes the app's versions.
    assert not route_allowed(standard, "POST", "/api/versions", True)
    assert route_allowed(sysadmin, "POST", "/api/versions/v_1/restore", True)
    # A normal box: the builder keeps them.
    assert route_allowed(standard, "POST", "/api/versions", False)


def test_router_is_mounted_in_the_app():
    from main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/versions" in paths
    assert "/api/versions/{version_id}/restore" in paths


# --------------------------------------------------------------- Leo's backup guidance

def test_leo_points_backup_requests_at_save_version():
    from app.agents.leonardo import export_rules

    for text in (export_rules.NO_APP_EXPORT_RULES, export_rules.REFUSAL_MESSAGE):
        assert "Save version" in text
        assert "History" in text


def test_a_mothership_without_the_endpoint_hides_the_section():
    """A box on 0.7.21 talking to a mothership that hasn't shipped LP-47 yet must not
    show an error in every History panel — the section just isn't there."""
    stub = StubMothership((404, {"error": "Not Found"}))
    r = make_client(stub).get("/api/versions")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "versions": []}


@pytest.mark.parametrize("answer", [(302, {}), (200, {}), (200, {"error": "html page"})])
def test_a_list_that_isnt_a_versions_answer_hides_the_section(answer):
    """Measured on the dev box 2026-11-16: the mothership answers the unbuilt route
    with a 302 to its login page. That must read as 'not available', never as a
    redirect or an empty-but-enabled list with a Save button that can't work."""
    r = make_client(StubMothership(answer)).get("/api/versions")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "versions": []}


def test_a_redirect_on_a_write_is_a_plain_error_not_a_redirect():
    r = make_client(StubMothership((302, {}))).post("/api/versions", json={"name": "v1"})
    assert r.status_code == 502
    assert r.json()["error"] == versions_router.UNREACHABLE_MESSAGE
