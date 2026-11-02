"""User-scoped Rails API access for CUSTOM agents — the "llamapress api" library.

This module is a LIBRARY, not an agent. LlamaBot deliberately registers no agent and
no chat mode that uses it: the reference implementation lives downstream in Leonardo
(`langgraph/agents/leo/nodes.py`, registered via `langgraph.local.json`, mounted into
the container as /app/app/user_agents). That end-to-end path — custom agent in the
client repo, library from the platform — is the product story, so keep it that way.

Custom agents need exactly two things:

    from app.lib.llamapress_api import (
        LlamaPressAPIState, standard_prompt_sections, standard_tools,
    )

    class MyState(LlamaPressAPIState):   # 1. api_token must be on your state schema
        ...                              #    (LangGraph drops fields not in the schema,
                                         #     which silently disables the tools)

    tools = standard_tools()             # 2. rails_api_request + read_uploaded_file

    sys_msg + standard_prompt_sections(state)  # 3. tells the model how to use both,
                                               #    plus the sign-in line when signed out

`read_uploaded_file` (LP-24) reads files the user dropped into chat (PDF, DOCX,
spreadsheets, text) through the gem's GET /llama_bot/uploads/:name, which does the
extraction. Custom modes send the user's token; rails_agent gets
`box_read_uploaded_file`, the same tool on the box-internal feed token, since it has
no per-user token and already has the filesystem via bash.

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

__all__ = [
    "LlamaPressAPIState",
    "box_read_uploaded_file",
    "make_rails_api_request",
    "rails_api_request",
    "rails_auth_prompt",
    "read_uploaded_file",
    "standard_prompt_sections",
    "standard_tools",
]

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

DEFAULT_RESPONSE_MAX_CHARS = 8000


def _env_max_chars(raw: str | None) -> int:
    """LLAMAPRESS_API_RESPONSE_MAX_CHARS, or 8000 if unset/garbled/not positive.
    A typo in .env must not turn the cap off."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_RESPONSE_MAX_CHARS
    return value if value > 0 else DEFAULT_RESPONSE_MAX_CHARS


# Rails errors are verbose (full HTML error pages in dev). Cap what re-enters context.
# The box-wide default stays 8k; one agent that reads long records raises its own
# cap with make_rails_api_request(max_chars=...) instead (LP-22).
RESPONSE_MAX_CHARS = _env_max_chars(os.getenv("LLAMAPRESS_API_RESPONSE_MAX_CHARS"))

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
    # Why there is (or isn't) a token this turn: "signed_in", "signed_out" (the
    # token fetch got 401), or "error" (network/CORS/5xx). Sent on every frame so
    # it's never stale — unlike api_token, which a checkpoint can carry over from
    # an earlier signed-in turn.
    rails_auth: NotRequired[str]


SIGN_IN_PATH = "/users/sign_in"

SIGNED_OUT_MESSAGE = (
    "The user is not signed in to the app, so I can't reach their data. Tell them "
    f"to sign in at {SIGN_IN_PATH} in the app panel, then ask again. Signing in "
    "is the whole fix — don't suggest waiting or trying again later."
)


def rails_auth_prompt(state) -> str:
    """A system-prompt line for custom modes: tell a signed-out user to sign in
    up front, before spending a tool call. Empty unless they're signed out, so
    the prompt (and its cache) is unchanged for everyone else.

        full_sys_msg = sys_msg + rails_auth_prompt(state)
    """
    if (state or {}).get("rails_auth") != "signed_out":
        return ""
    return (
        "\n\nThe user is currently NOT signed in to the app. If they ask about "
        f"their app data, first tell them to sign in at {SIGN_IN_PATH} in the app "
        "panel and then ask again. Signing in is the whole fix."
    )


def _user_token(runtime) -> tuple[str | None, str | None]:
    """(token, None), or (None, a message for the model) when there is no usable one."""
    state = getattr(runtime, "state", None) or {}
    rails_auth = state.get("rails_auth")
    # Checked before the token: a token checkpointed on an earlier turn can
    # outlive the session, and rails_auth is this turn's answer.
    if rails_auth == "signed_out":
        return None, SIGNED_OUT_MESSAGE
    token = state.get("api_token")
    if not token and rails_auth == "error":
        return None, (
            "Couldn't get a sign-in token from the app this turn (the app didn't "
            "answer the token request — it may be restarting). Tell the user that "
            "plainly; if it keeps happening, reloading the page usually fixes it."
        )
    if not token:
        # No token and no rails_auth reason: the agent's state schema forgot to
        # declare the fields (subclass LlamaPressAPIState), or an old frontend. Fail loudly: silently proceeding
        # unauthenticated would look like a permissions bug rather than a wiring one.
        return None, (
            "No API token available for this session, so I can't make authenticated "
            "requests to your app right now. This is a setup issue, not a permissions "
            "one — tell the user to report it."
        )
    return token, None


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


# Appended when a successful reply is cut, so the model fetches the rest instead
# of answering from the first slice.
PAGINATION_HINT = (
    "The reply was too long and was cut. To get the rest, ask for less per request: "
    "use the endpoint's pagination or filter params (e.g. ?page=2, ?per_page=, ?q=) or "
    "request one record or section at a time. Don't answer as if this is everything."
)


def _rails_api_request(method, path, runtime, body, max_chars: int) -> str:
    """HTTP against the Rails app as the signed-in user. See module docstring."""
    token, problem = _user_token(runtime)
    if problem:
        return problem

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

    if not response.is_success:
        # The raised cap is for long records, not for Rails' HTML error pages.
        return f"HTTP {response.status_code}\n{_truncate(response.text, min(max_chars, RESPONSE_MAX_CHARS))}"
    text = response.text
    if len(text) > max_chars:
        return (
            f"HTTP {response.status_code}\n{text[:max_chars]}\n\n"
            f"[truncated {len(text) - max_chars} more chars of {len(text)}. {PAGINATION_HINT}]"
        )
    return f"HTTP {response.status_code}\n{text}"


def make_rails_api_request(max_chars: int = 40000):
    """rails_api_request with its own reply cap, for an agent that reads long
    records (a report page, a big JSON list):

        tools = [make_rails_api_request(max_chars=40000), read_uploaded_file]

    Same name, description and guards; only the cap differs, and only for 2xx
    replies. Error pages keep the box default. Every char returned re-enters the
    model's context on every later turn, so raise it for the agent that needs it,
    not box-wide.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError(f"max_chars must be a positive int, got {max_chars!r}")

    @tool("rails_api_request", description=RAILS_API_REQUEST_DESCRIPTION)
    def rails_api_request(
        method: str,
        path: str,
        runtime: ToolRuntime,
        body: dict | None = None,
    ) -> str:
        """HTTP against the Rails app as the signed-in user. See module docstring."""
        return _rails_api_request(method, path, runtime, body, max_chars)

    return rails_api_request


# The shared, box-default tool (8k unless LLAMAPRESS_API_RESPONSE_MAX_CHARS says otherwise).
rails_api_request = make_rails_api_request(max_chars=RESPONSE_MAX_CHARS)


# --- read_uploaded_file (LP-24) ---------------------------------------------
#
# Files dropped into chat are saved to the Rails app's app/imports; the model only
# gets their names in an <UPLOADED_FILES> block. The gem's UploadsController does
# the extraction (PDF, DOCX, XLSX/XLS/XLSM, CSV, text) and pages the result, so
# every mode reads files the same way instead of improvising a script per type.

UPLOADS_PATH = "/llama_bot/uploads"

# One gem page is 20k chars; leave room for the header lines around it.
UPLOAD_RESPONSE_MAX_CHARS = 24000

READ_UPLOADED_FILE_DESCRIPTION = """Read the text of a file the user uploaded into chat.

Uploaded files are named in the <UPLOADED_FILES> block of the user's message. This
returns their text: PDF (text layer), DOCX, XLSX/XLS/XLSM (every sheet, one row per
line, tab-separated), CSV and plain text.

Args:
    name: The file name, e.g. "leads.xlsx" (a path like "app/imports/leads.xlsx" also works).
    offset: Character offset to start from. Long files come back one page at a
            time; the result tells you the offset of the next page.
    q: Optional text to search for. Returns every matching line with its line
       number instead of a page — use it to find rows in a big file.

Returns:
    A header (status, size, which part you are seeing) followed by the text.
    If the name is wrong, the files that ARE there are listed.
"""

_STATUS_NOTES = {
    "empty": "The file has no extractable text (a scanned PDF or image-only document has no text layer).",
    "unsupported": "Text extraction isn't supported for this file type.",
    "failed": "The file couldn't be parsed (it may be corrupt or password-protected).",
}


def _upload_name(name: str) -> str | None:
    """The bare file name, or None if it can't name a file. The server only
    matches names against a listing of app/imports, so this is about giving the
    model a useful answer, not about containment."""
    base = (name or "").replace("\\", "/").strip().rsplit("/", 1)[-1].strip()
    if base in ("", ".", ".."):
        return None
    return base


def _format_upload(payload: dict, offset: int) -> str:
    name = payload.get("name", "?")
    status = payload.get("status", "?")
    total = payload.get("total_chars", 0) or 0
    lines = [f"{name} — status: {status}, {total} characters of text."]
    if status in _STATUS_NOTES:
        lines.append(_STATUS_NOTES[status])

    if "matches" in payload:
        matches = payload.get("matches") or []
        lines.append(f"{len(matches)} line(s) matching {payload.get('q', '')!r}:")
        lines.extend(f"L{m.get('line')}: {m.get('text', '')}" for m in matches)
        return _truncate("\n".join(lines), UPLOAD_RESPONSE_MAX_CHARS)

    text = payload.get("text") or ""
    start = payload.get("offset", offset) or 0
    next_offset = payload.get("next_offset")
    if total:
        lines.append(f"Showing characters {start}–{start + len(text)}.")
    body = "\n".join(lines) + ("\n\n" + text if text else "")
    footer = (
        f"\n\n[More text follows: call read_uploaded_file with offset={next_offset}.]"
        if next_offset is not None
        else ""
    )
    return _truncate(body, UPLOAD_RESPONSE_MAX_CHARS - len(footer)) + footer


def _fetch_upload(name: str, offset: int, q: str | None, authorization: str) -> str:
    """GET the page (or search) from Rails and render it for the model."""
    from urllib.parse import quote

    bare = _upload_name(name)
    if bare is None:
        return "Give the uploaded file's name, e.g. read_uploaded_file(name=\"leads.xlsx\")."

    try:
        offset = max(int(offset or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    params = {"offset": str(offset)}
    if q:
        params["q"] = str(q)

    headers = {"Authorization": authorization, "Accept": "application/json"}
    logger.info(f"[llamapress_api] GET {UPLOADS_PATH}/{bare}")

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False) as client:
            # quote(safe=""): the name stays one path segment ("/" and "?" can't
            # reach the router as anything but part of the name).
            response = client.get(
                f"{RAILS_BASE_URL}{UPLOADS_PATH}/{quote(bare, safe='')}",
                params=params,
                headers=headers,
            )
            if response.status_code == 404:
                listing = client.get(f"{RAILS_BASE_URL}{UPLOADS_PATH}", headers=headers)
                names = []
                if listing.status_code == 200:
                    names = [f.get("name") for f in (listing.json().get("files") or []) if isinstance(f, dict)]
                if names:
                    return f"No uploaded file named {bare!r}. Uploaded files: " + ", ".join(names)
                if listing.status_code == 404:
                    # The index route itself is missing: a gem that predates LP-24.
                    return (
                        f"Couldn't read {bare!r}: this app's llama_bot_rails gem is too old "
                        "to serve uploaded files. Tell the user it needs a platform update."
                    )
                return f"No uploaded file named {bare!r}, and no uploaded files were found."
    except (httpx.RequestError, ValueError) as e:
        logger.warning(f"[llamapress_api] uploads request failed: {e.__class__.__name__}")
        return f"Could not reach the Rails app ({e.__class__.__name__}). It may be starting up or down."

    if response.status_code == 401:
        return "The app refused the request as unauthenticated (the sign-in token may have expired — they last 30 minutes)."
    if response.status_code == 403:
        return f"HTTP 403: the app doesn't allow this user to read {bare!r}. Report that plainly."
    if response.status_code != 200:
        return f"HTTP {response.status_code}\n{_truncate(response.text, 2000)}"

    try:
        payload = response.json()
    except ValueError:
        return f"HTTP 200 but the app sent something that isn't JSON:\n{_truncate(response.text, 2000)}"
    return _format_upload(payload if isinstance(payload, dict) else {}, offset)


@tool("read_uploaded_file", description=READ_UPLOADED_FILE_DESCRIPTION)
def read_uploaded_file(
    name: str,
    runtime: ToolRuntime,
    offset: int = 0,
    q: str | None = None,
) -> str:
    """Custom modes: read an upload as the signed-in user (uploads_authorizer applies)."""
    token, problem = _user_token(runtime)
    if problem:
        return problem
    return _fetch_upload(name, offset, q, f"LlamaBot {token}")


@tool("read_uploaded_file", description=READ_UPLOADED_FILE_DESCRIPTION)
def box_read_uploaded_file(name: str, offset: int = 0, q: str | None = None) -> str:
    """rails_agent: same tool on the box-internal feed token, no per-user token
    needed. Only for agents that can already read app/imports (bash, or the research
    helper's read_file) — it is not wider than what they hold, and it skips the gem's
    uploads_authorizer."""
    from app.services.rails_error_feed import FEED_SCHEME, feed_token

    box = feed_token()
    if not box:
        return (
            "This box has no SECRET_KEY_BASE shared with Rails, so the uploads feed "
            "isn't reachable. Read the file from app/imports with bash instead, or, if "
            "you have no shell, report this back to the main agent."
        )
    return _fetch_upload(name, offset, q, f"{FEED_SCHEME} {box}")


def standard_tools() -> list:
    """The shared tools for a custom mode: `tools = standard_tools()`.

    Both are bounded by the signed-in user's Rails permissions. A new list each
    call, so a mode can append its own tools without touching anyone else's.
    """
    return [rails_api_request, read_uploaded_file]


STANDARD_TOOLS_PROMPT = """

## Your tools for this app

- `rails_api_request` calls this app's JSON API as the signed-in user. Their
  permissions apply: a 403 is the correct answer, report it plainly.
- `read_uploaded_file` reads a file the user uploaded into chat (named in the
  <UPLOADED_FILES> block): PDF, DOCX, spreadsheets, CSV and text. Read it before
  answering questions about it. Long files come back in pages; use `offset` for the
  next page, or `q` to find matching lines."""


def standard_prompt_sections(state=None) -> str:
    """The system-prompt text that goes with standard_tools():

        full_sys_msg = sys_msg + standard_prompt_sections(state)

    Includes rails_auth_prompt(state), so a signed-out user is told to sign in.
    """
    return STANDARD_TOOLS_PROMPT + rails_auth_prompt(state)
