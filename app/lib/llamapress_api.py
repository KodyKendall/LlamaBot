"""User-scoped Rails API access for CUSTOM agents — the "llamapress api" library.

This module is a LIBRARY, not an agent. LlamaBot deliberately registers no agent and
no chat mode that uses it: the reference implementation lives downstream in Leonardo
(`langgraph/agents/leo/nodes.py`, registered via `langgraph.local.json`, mounted into
the container as /app/app/user_agents). That end-to-end path — custom agent in the
client repo, library from the platform — is the product story, so keep it that way.

Custom agents need exactly two things:

    from app.lib.llamapress_api import LlamaPressAPIState, rails_api_request

    class MyState(LlamaPressAPIState):   # 1. api_token must be on your state schema
        ...                              #    (LangGraph drops fields not in the schema,
                                         #     which silently disables the tool)

    tools = [rails_api_request]          # 2. give the agent the tool

Why this exists: every other Rails-touching tool in this repo (`bash_command`,
`rails_api_sh`) shells into the Rails container and gets unrestricted ActiveRecord.
That's fine for `engineer`, but it means "let a low-privilege user ask Leo about
their data" had no safe answer. This tool is that answer.

The authorization is NOT implemented here. It is inherited, and that's the point:

  1. Rails mints a signed, 30-min token carrying `user_id` (the gem's
     `message_verifier(:llamabot_ws)`). LlamaBot cannot forge one. The browser
     fetches it from `GET /llama_bot/agent/token` (Devise session required) and the
     chat frontend puts it on the WebSocket frame as `api_token`, which the request
     handler copies into state — but only for schemas that declare it.
  2. The gem's AgentAuth verifies the signature, resolves the user, and signs them
     in via warden — so `current_user` in the controller is genuinely that person.
  3. `llama_bot_allow :index, :show` in the controller allowlists which actions a
     token-authed request may reach AT ALL. Anything else is 403 before any policy
     runs.
  4. Pundit then narrows per-user as it always has.

So `DELETE /api/users/1` fails twice over: `destroy` isn't allowlisted, and the
token's user isn't an admin. Widening reach is a Rails-side act (`llama_bot_allow`),
never a Python-side one. Do not add an escape hatch to this file.

The guards below are about SSRF, not authorization: keep the request pointed at the
Rails app so the token can't be posted to an attacker's host.

Full writeup: docs/dev/user_api_mode.md.
"""
import logging
import os

import httpx
from langchain.tools import ToolRuntime, tool
from langgraph.prebuilt.chat_agent_executor import AgentState
from typing_extensions import NotRequired

logger = logging.getLogger(__name__)

__all__ = ["LlamaPressAPIState", "rails_api_request"]

# The Rails service on the compose network (see Leonardo/docker-compose-dev.yml:
# service `llamapress`, port 3000). Overridable for other deployments.
RAILS_BASE_URL = os.getenv("RAILS_BASE_URL", "http://llamapress:3000").rstrip("/")

ALLOWED_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})

# Only the agent-facing API namespace is reachable. This is defense-in-depth, and it
# is NOT redundant with the Rails-side gates — it patches a real hole:
#
# `llama_bot_allow` only constrains controllers that include LlamaBotRails::AgentAuth.
# A controller that never opted in is gated by the app's OWN auth — and the Leonardo
# skeleton ships with `before_action :authenticate_user!` commented out ("AUTHENTICATION
# IS DISABLED BY DEFAULT FOR NEW PROJECTS", rails/app/controllers/application_controller.rb).
# Verified against the dev box: `GET /users` with no token at all returns 500, i.e. it
# routed into the controller — ungated. Without this prefix check, this tool
# would reach every such route and the per-account scoping would be a fiction.
#
# `/api/` is where the agent-facing controllers live by convention (see
# Leonardo/rails/config/routes.rb + Api::UsersController). Note this is a CONVENTION,
# not a proof: a controller added under /api/ that forgets `include AgentAuth` is
# ungated too, unless the app enables authentication. See docs/dev/user_api_mode.md.
ALLOWED_PATH_PREFIX = "/api/"

# Rails errors are verbose (full HTML error pages in dev). Cap what re-enters context.
RESPONSE_MAX_CHARS = 8000

REQUEST_TIMEOUT_SECONDS = 30

RAILS_API_REQUEST_DESCRIPTION = """Make an HTTP request to the Rails app's JSON API, authenticated as the signed-in user.

Use this to read and change data through the app's own API, exactly as the current
user could in their browser. Their permissions apply automatically: if they aren't
allowed to do something, the request comes back 403 and that answer is correct —
report it plainly, don't try to work around it.

Args:
    method: GET, POST, PATCH, PUT, or DELETE.
    path: App-relative path starting with "/", e.g. "/api/users" or "/api/users/42".
          May include a query string: "/api/users?q=alice".
    body: Optional dict, sent as a JSON request body (POST/PATCH/PUT).

Returns:
    The HTTP status line followed by the response body.

Notes:
    - Only allow-listed endpoints are reachable. A 403 saying an action "isn't
      white-listed" means the app has not exposed it — that is a deliberate limit,
      not a bug to route around. Tell the user what you tried and what came back.
    - You cannot reach any host other than the Rails app.
"""


class LlamaPressAPIState(AgentState):
    """Base state for custom agents that call the Rails API as the signed-in user.

    Subclass this (or copy the field verbatim). The field name `api_token` is part of
    the wire contract: the frontend puts it on the WebSocket frame under that key, and
    the request handler copies frame keys into state only if the schema declares them.
    Never put the token in a prompt or a log — it is a bearer credential for that user.
    """

    api_token: NotRequired[str]


def _truncate(text: str, max_chars: int = RESPONSE_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[truncated {len(text) - max_chars} more chars]"


def _reject_path(path: str) -> str | None:
    """Return an error string if `path` could leave the Rails app, else None.

    The token is a bearer credential: any request we send carries it in a header, so
    an attacker-influenced path that redirects the request to another origin would
    hand it over. Cheapest defense is to refuse anything that isn't a plain
    app-relative path.
    """
    if not path.startswith("/"):
        return "path must start with '/' (it is app-relative, e.g. '/api/users')"
    if path.startswith("//"):
        # "//evil.com/x" is protocol-relative — urljoin/httpx would resolve it off-host.
        return "path must not start with '//'"
    if "://" in path:
        return "path must not contain a scheme — only the Rails app is reachable"
    if "\\" in path:
        return "path must not contain backslashes"
    if ".." in path:
        # "/api/../users" normalizes out of the namespace at the server.
        return "path must not contain '..'"
    if not path.startswith(ALLOWED_PATH_PREFIX):
        return (
            f"only the app's API namespace is reachable — path must start with "
            f"'{ALLOWED_PATH_PREFIX}' (got {path!r})"
        )
    return None


@tool(description=RAILS_API_REQUEST_DESCRIPTION)
def rails_api_request(
    method: str,
    path: str,
    runtime: ToolRuntime,
    body: dict | None = None,
) -> str:
    """HTTP against the Rails app as the signed-in user. See module docstring."""
    token = (runtime.state or {}).get("api_token")
    if not token:
        # Reached when the chat frame carried no api_token — e.g. the user isn't
        # signed into the Rails app, or the agent's state schema forgot to declare
        # the field (subclass LlamaPressAPIState). Fail loudly: silently proceeding
        # unauthenticated would look like a permissions bug rather than a wiring one.
        return (
            "No API token available for this session, so I can't make authenticated "
            "requests to your app right now. This is a setup issue, not a permissions "
            "one — tell the user to report it."
        )

    method = (method or "").strip().upper()
    if method not in ALLOWED_METHODS:
        return f"Unsupported method {method!r}. Use one of: {', '.join(sorted(ALLOWED_METHODS))}."

    if path_error := _reject_path(path or ""):
        return f"Invalid path: {path_error}"

    url = f"{RAILS_BASE_URL}{path}"
    # `LlamaBot` is the scheme the gem's AgentAuth looks for (AUTH_SCHEME).
    headers = {
        "Authorization": f"LlamaBot {token}",
        "Accept": "application/json",
    }

    # Log the path but NEVER the token or the headers.
    logger.info(f"[llamapress_api] {method} {path}")

    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            # Do not follow redirects: a redirect off-origin would replay the
            # Authorization header at the new host.
            follow_redirects=False,
        ) as client:
            response = client.request(
                method,
                url,
                headers=headers,
                json=body if body is not None else None,
            )
    except httpx.RequestError as e:
        # Connection-level failure — the exception repr can't contain the token
        # (it's in headers, not the URL), so this is safe to surface.
        logger.warning(f"[llamapress_api] request failed: {e.__class__.__name__}")
        return f"Could not reach the Rails app ({e.__class__.__name__}). It may be starting up or down."

    if response.is_redirect:
        # Almost always Devise bouncing an unauthenticated request to /login, which
        # means the token didn't authenticate. Say so rather than following it.
        return (
            f"HTTP {response.status_code} (redirect to {response.headers.get('location', '?')})\n"
            "The request wasn't authenticated — the token may have expired (they last 30 minutes)."
        )

    return f"HTTP {response.status_code}\n{_truncate(response.text)}"
