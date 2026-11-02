"""The llamapress_api library (app/lib/llamapress_api.py) — user-scoped Rails HTTP.

This is a LIBRARY for custom agents (the reference implementation lives downstream
in Leonardo's langgraph/agents/leo, registered via the langgraph.local.json overlay).
LlamaBot itself deliberately registers no agent and no chat mode that uses it.

What actually enforces "this user can only touch their own stuff" lives in RAILS
(the gem's signed token -> warden sign-in -> `llama_bot_allow` -> Pundit), and is
covered by the gem's specs. There is deliberately no authorization logic in Python
to test.

So what's left here is the stuff that would silently DEFEAT that design from this
side, and each test below maps to one such way:

  * the token must actually reach the tool (a dropped state field = the tool is
    dead, or worse, quietly unauthenticated)
  * the token must never leave for a host that isn't Rails (it's a bearer
    credential — an off-host request hands the user's account away)
  * LlamaBot must NOT quietly re-grow a built-in mode around it (the boundary is
    the product story: platform ships the library, the client repo ships the agent)
"""
import httpx
import pytest

from app.lib import llamapress_api
from app.lib.llamapress_api import (
    RAILS_BASE_URL,
    LlamaPressAPIState,
    rails_api_request,
)


class _Runtime:
    """Stand-in for ToolRuntime — the tool only reads `.state`."""

    def __init__(self, state):
        self.state = state
        self.tool_call_id = "test-call"


def _invoke(method="GET", path="/api/users", body=None, token="signed-token", rails_auth=None):
    state = {"api_token": token} if token is not None else {}
    if rails_auth is not None:
        state["rails_auth"] = rails_auth
    return rails_api_request.func(
        method=method, path=path, body=body, runtime=_Runtime(state)
    )


@pytest.fixture
def captured(monkeypatch):
    """Capture the outbound request instead of making one. Returns a dict that
    fills in with the request the tool tried to send."""
    seen = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, json=None):
            seen.update(method=method, url=url, headers=headers, json=json)
            return httpx.Response(
                200,
                text='[{"id":1,"email":"a@b.com"}]',
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(llamapress_api.httpx, "Client", _FakeClient)
    return seen


# --- the token reaches Rails, correctly ------------------------------------

def test_token_is_sent_as_the_gem_auth_scheme(captured):
    """The gem's AgentAuth only recognises `Authorization: LlamaBot <token>`
    (AUTH_SCHEME). Send `Bearer` and every request 401s."""
    _invoke(token="signed-token")
    assert captured["headers"]["Authorization"] == "LlamaBot signed-token"


def test_request_goes_to_the_rails_app(captured):
    _invoke(path="/api/users?q=alice")
    assert captured["url"] == f"{RAILS_BASE_URL}/api/users?q=alice"


def test_body_is_sent_as_json(captured):
    _invoke(method="POST", path="/api/users", body={"email": "a@b.com"})
    assert captured["json"] == {"email": "a@b.com"}
    assert captured["method"] == "POST"


def test_missing_token_does_not_send_an_unauthenticated_request(captured):
    """Without a token the tool must NOT fall through to an anonymous request —
    that would read as a permissions result when it's really a wiring failure."""
    result = _invoke(token=None)
    assert "url" not in captured, "made a request despite having no token"
    assert "setup issue" in result


# --- signed out is the user's fix, not a setup bug (LP-23) -----------------
# Assert the routing (which message family comes back), never exact wording.

def test_signed_out_user_is_told_to_sign_in(captured):
    result = _invoke(token=None, rails_auth="signed_out")
    assert "url" not in captured
    assert "/users/sign_in" in result
    assert "setup issue" not in result


def test_signed_out_wins_over_a_stale_checkpointed_token(captured):
    """api_token lives in the checkpoint, so a token from an earlier signed-in
    turn can outlive the session. The frame's rails_auth is this turn's truth."""
    result = _invoke(token="old-token", rails_auth="signed_out")
    assert "url" not in captured, "sent a request with a stale token"
    assert "/users/sign_in" in result


def test_token_fetch_error_is_reported_as_such(captured):
    result = _invoke(token=None, rails_auth="error")
    assert "url" not in captured
    assert "setup issue" not in result
    assert "/users/sign_in" not in result


def test_signed_out_prompt_line_only_when_signed_out():
    assert "/users/sign_in" in llamapress_api.rails_auth_prompt({"rails_auth": "signed_out"})
    assert llamapress_api.rails_auth_prompt({"rails_auth": "signed_in"}) == ""
    assert llamapress_api.rails_auth_prompt({}) == ""


# --- the token can't leak off-host (SSRF) ----------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "//evil.com/steal",             # protocol-relative
        "https://evil.com/steal",       # absolute
        "http://evil.com/steal",
        "api/users",                    # not app-relative
        "/api\\evil",                   # backslash trick
    ],
)
def test_paths_that_could_leave_the_rails_app_are_refused(captured, path):
    result = _invoke(path=path)
    assert "url" not in captured, f"path {path!r} produced an outbound request"
    assert "Invalid path" in result


# --- the path boundary ------------------------------------------------------
#
# Why this exists: `llama_bot_allow` only gates controllers that include
# AgentAuth, and the Leonardo skeleton ships with the app's own
# `authenticate_user!` commented out. Verified on the dev box: `GET /users` with
# NO token returns 500 — it routed into the controller, ungated. So restricting
# the tool to the agent-facing namespace is load-bearing, not decoration.

@pytest.mark.parametrize(
    "path",
    [
        "/users",              # the ungated app route this check exists for
        "/users/1",
        "/",
        "/admin/things",
        "/llama_bot/agent",    # the engine's own routes
        "/apifoo",             # prefix must be "/api/", not "/api"
        "/api/../users",       # normalizes out of the namespace server-side
    ],
)
def test_routes_outside_the_api_namespace_are_refused(captured, path):
    result = _invoke(path=path)
    assert "url" not in captured, f"path {path!r} produced an outbound request"
    assert "Invalid path" in result


def test_the_api_namespace_is_reachable(captured):
    """The guard must not be so tight the tool is useless."""
    _invoke(path="/api/users")
    assert captured["url"].endswith("/api/users")


def test_redirects_are_not_followed(captured):
    """A redirect would replay the Authorization header at the new origin."""
    _invoke()
    assert captured["client_kwargs"]["follow_redirects"] is False


def test_redirect_response_is_reported_not_chased(monkeypatch):
    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, json=None):
            return httpx.Response(
                302,
                headers={"location": "/users/sign_in"},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(llamapress_api.httpx, "Client", _FakeClient)
    result = _invoke()
    assert "302" in result and "expired" in result


def test_unsupported_method_is_refused(captured):
    result = _invoke(method="TRACE")
    assert "url" not in captured
    assert "Unsupported method" in result


def test_token_is_never_echoed_into_the_response(monkeypatch):
    """Tool output re-enters the LLM context and the transcript. The token must
    not ride along — a 403 body is shown to the user verbatim."""
    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, json=None):
            return httpx.Response(403, text="forbidden", request=httpx.Request(method, url))

    monkeypatch.setattr(llamapress_api.httpx, "Client", _FakeClient)
    result = _invoke(token="super-secret-token")
    assert "super-secret-token" not in result


def test_a_403_is_surfaced_as_a_normal_result(monkeypatch):
    """Pundit/allowlist denials are the expected path, not an exception."""
    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, json=None):
            return httpx.Response(
                403,
                text='{"error":"Action \'destroy\' isn\'t white-listed for LlamaBot."}',
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(llamapress_api.httpx, "Client", _FakeClient)
    result = _invoke(method="DELETE", path="/api/users/1")
    assert "HTTP 403" in result
    assert "white-listed" in result


# --- the wire contract ------------------------------------------------------

def test_state_base_declares_api_token():
    """LangGraph filters state to the schema, so an undeclared `api_token` is
    dropped between the WS frame and the tool — the tool fails closed and looks
    like a permissions bug. Custom agents subclass this to stay wired."""
    assert "api_token" in LlamaPressAPIState.__annotations__


def test_state_base_declares_rails_auth():
    """Same filter, same failure: without the field a signed-out user falls
    back to the misleading "setup issue" message (LP-23)."""
    assert "rails_auth" in LlamaPressAPIState.__annotations__


# --- the platform/client boundary -------------------------------------------

def test_llamabot_ships_no_builtin_agent_or_mode_for_this_library():
    """The working agent lives in the CLIENT repo (Leonardo leo, via the
    langgraph.local.json overlay). If LlamaBot re-grows a built-in `user_api`
    mode or registers a graph around this tool, the separation this library
    exists for is gone — do it deliberately or not at all."""
    import json
    from pathlib import Path

    from app.permissions import BUILTIN_AGENT_MODE_KEYS, MODE_AGENTS

    assert "user_api" not in BUILTIN_AGENT_MODE_KEYS
    assert "user_api" not in MODE_AGENTS

    base = json.loads((Path(__file__).parent.parent / "langgraph.json").read_text())
    assert "user_api_agent" not in base["graphs"]


# --- LP-22: the reply cap is raisable per agent -------------------------------
# A client's 22 KB report page came back as 31,155 chars of JSON (Rails escapes
# < > & to < etc.), and the 8k cap dropped 5 of its 6 sections.

REPORT_31K = '{"html":"' + ("\\u003cp\\u003eSection\\u003c/p\\u003e" * 1000)[:31_140] + '"}'


def _serving(monkeypatch, text, status=200):
    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, json=None):
            return httpx.Response(status, text=text, request=httpx.Request(method, url))

    monkeypatch.setattr(llamapress_api.httpx, "Client", _FakeClient)


def _call(tool_obj):
    return tool_obj.func(
        method="GET", path="/api/reports/1", body=None,
        runtime=_Runtime({"api_token": "signed-token"}),
    )


def test_default_tool_still_caps_a_31k_reply_at_8000(monkeypatch):
    assert len(REPORT_31K) > 31_000
    _serving(monkeypatch, REPORT_31K)

    result = _call(rails_api_request)

    assert llamapress_api.RESPONSE_MAX_CHARS == 8000
    assert REPORT_31K[:8000] in result
    assert REPORT_31K not in result
    assert "truncated" in result


def test_raised_tool_returns_the_whole_31k_reply(monkeypatch):
    _serving(monkeypatch, REPORT_31K)

    result = _call(llamapress_api.make_rails_api_request(max_chars=40_000))

    assert REPORT_31K in result
    assert "truncated" not in result


def test_raised_tool_is_a_drop_in_replacement():
    raised = llamapress_api.make_rails_api_request(max_chars=40_000)
    assert raised.name == "rails_api_request"
    assert raised.description == rails_api_request.description
    assert raised is not rails_api_request


def test_raised_tool_keeps_every_guard(monkeypatch, captured):
    raised = llamapress_api.make_rails_api_request(max_chars=40_000)
    result = raised.func(method="GET", path="//evil.com/x", body=None,
                         runtime=_Runtime({"api_token": "signed-token"}))
    assert "url" not in captured
    assert "Invalid path" in result
    result = raised.func(method="GET", path="/api/users", body=None,
                         runtime=_Runtime({"rails_auth": "signed_out"}))
    assert "/users/sign_in" in result


def test_raised_cap_does_not_apply_to_error_pages(monkeypatch):
    """The 8k default exists for Rails' huge HTML error pages; opting in to long
    reports must not also let a 500 page flood the context."""
    _serving(monkeypatch, "<html>" + "x" * 30_000, status=500)

    result = _call(llamapress_api.make_rails_api_request(max_chars=40_000))

    assert len(result) < 9_000


def test_truncation_note_points_at_pagination(monkeypatch):
    _serving(monkeypatch, REPORT_31K)
    note = _call(rails_api_request)[8000:]
    assert "page" in note.lower()


@pytest.mark.parametrize("bad", [0, -1, "40000", 1.5, True])
def test_factory_rejects_a_bad_cap(bad):
    with pytest.raises(ValueError):
        llamapress_api.make_rails_api_request(max_chars=bad)


@pytest.mark.parametrize("raw, expected", [
    (None, 8000), ("", 8000), ("20000", 20000), (" 12000 ", 12000),
    ("lots", 8000), ("0", 8000), ("-5", 8000),
])
def test_env_cap_parsing_falls_back_to_8000(raw, expected):
    assert llamapress_api._env_max_chars(raw) == expected
