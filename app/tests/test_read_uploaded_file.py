"""LP-24: read_uploaded_file — the shared tool every Leo mode uses to read uploads.

Files a user drops into chat are saved to the Rails app's app/imports. The gem's
UploadsController (GET /llama_bot/uploads/:name) turns them into paged text;
these tests pin the Python side of that contract:

  * the right credential reaches Rails (user token for custom modes, the box
    feed token for rails_agent), and nothing is sent without one
  * the name can't steer the request anywhere but the uploads endpoint
  * paging / search hints come back in a shape the model can act on
  * custom modes get both tools and the prompt from one call each
  * rails_agent carries the tool and the "use the tool first" directive

Assert structure, never wording.
"""
from unittest.mock import patch
from urllib.parse import unquote

import httpx
import pytest

from app.lib import llamapress_api
from app.lib.llamapress_api import (
    RAILS_BASE_URL,
    box_read_uploaded_file,
    rails_api_request,
    read_uploaded_file,
    standard_prompt_sections,
    standard_tools,
)


class _Runtime:
    def __init__(self, state):
        self.state = state
        self.tool_call_id = "test-call"


def _invoke(name="report.pdf", token="signed-token", rails_auth=None, **kwargs):
    state = {"api_token": token} if token is not None else {}
    if rails_auth is not None:
        state["rails_auth"] = rails_auth
    return read_uploaded_file.func(name=name, runtime=_Runtime(state), **kwargs)


def _responder(routes):
    """Fake httpx.Client: maps a URL path to (status, json) and records requests."""
    seen = []

    class _FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            seen.append({"url": url, "params": params or {}, "headers": headers or {}, "kwargs": self.kwargs})
            path = url[len(RAILS_BASE_URL):]
            status, body = routes.get(unquote(path), (404, {"error": "no"}))
            return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    return _FakeClient, seen


@pytest.fixture
def rails(monkeypatch):
    routes = {}
    client, seen = _responder(routes)
    monkeypatch.setattr(llamapress_api.httpx, "Client", client)
    return routes, seen


PAGE = {
    "name": "report.pdf", "status": "extracted", "total_chars": 50000,
    "offset": 0, "text": "Quarterly revenue up", "next_offset": 20000,
}


# --- credentials -------------------------------------------------------------

def test_user_token_is_sent_as_the_gem_auth_scheme(rails):
    routes, seen = rails
    routes["/llama_bot/uploads/report.pdf"] = (200, PAGE)

    _invoke()

    assert seen[0]["headers"]["Authorization"] == "LlamaBot signed-token"
    assert seen[0]["kwargs"].get("follow_redirects") is False


def test_signed_out_user_is_told_to_sign_in_without_a_request(rails):
    _routes, seen = rails
    result = _invoke(token="stale", rails_auth="signed_out")

    assert seen == []
    assert "/users/sign_in" in result


def test_missing_token_sends_nothing(rails):
    _routes, seen = rails
    result = _invoke(token=None)

    assert seen == []
    assert "setup issue" in result


def test_box_variant_uses_the_feed_token_and_needs_no_state(rails, monkeypatch):
    monkeypatch.setenv("SECRET_KEY_BASE", "box-secret")
    routes, seen = rails
    routes["/llama_bot/uploads/report.pdf"] = (200, PAGE)

    box_read_uploaded_file.func(name="report.pdf")

    scheme, token = seen[0]["headers"]["Authorization"].split(" ", 1)
    assert scheme == "LlamaBotFeed"
    from app.services.rails_error_feed import feed_token
    assert token == feed_token()


def test_box_variant_without_a_shared_secret_sends_nothing(rails, monkeypatch):
    monkeypatch.delenv("SECRET_KEY_BASE", raising=False)
    _routes, seen = rails

    result = box_read_uploaded_file.func(name="report.pdf")

    assert seen == []
    assert "bash" in result  # points the engineer at the fallback


def test_both_variants_share_the_tool_name():
    assert read_uploaded_file.name == "read_uploaded_file"
    assert box_read_uploaded_file.name == "read_uploaded_file"


# --- the name can only pick a file -------------------------------------------

def test_a_saved_path_is_reduced_to_its_file_name(rails):
    """The <UPLOADED_FILES> block shows 'saved to app/imports/x'; models pass that."""
    routes, seen = rails
    routes["/llama_bot/uploads/report.pdf"] = (200, PAGE)

    _invoke(name="app/imports/report.pdf")

    assert unquote(seen[0]["url"]) == f"{RAILS_BASE_URL}/llama_bot/uploads/report.pdf"


@pytest.mark.parametrize("name", ["..", ".", "", "  "])
def test_names_that_are_not_files_are_refused(rails, name):
    _routes, seen = rails
    _invoke(name=name)
    assert seen == []


def test_name_is_url_encoded_so_it_stays_one_path_segment(rails):
    routes, seen = rails
    routes["/llama_bot/uploads/q3 #1?.csv"] = (200, {**PAGE, "name": "q3 #1?.csv"})

    _invoke(name="q3 #1?.csv")

    url = seen[0]["url"]
    assert url.startswith(f"{RAILS_BASE_URL}/llama_bot/uploads/")
    assert "#" not in url and "?" not in url


# --- shape of what comes back --------------------------------------------------

def test_page_text_and_next_offset_reach_the_model(rails):
    routes, _seen = rails
    routes["/llama_bot/uploads/report.pdf"] = (200, PAGE)

    result = _invoke()

    assert "Quarterly revenue up" in result
    assert "offset=20000" in result


def test_offset_and_q_are_forwarded(rails):
    routes, seen = rails
    routes["/llama_bot/uploads/report.pdf"] = (200, {**PAGE, "q": "revenue", "matches": [{"line": 7, "text": "revenue 42"}]})

    result = _invoke(offset=20000, q="revenue")

    assert seen[0]["params"] == {"offset": "20000", "q": "revenue"}
    assert "7" in result and "revenue 42" in result


def test_unsupported_type_is_reported_with_its_status(rails):
    routes, _seen = rails
    routes["/llama_bot/uploads/deck.pptx"] = (200, {**PAGE, "name": "deck.pptx", "status": "unsupported", "text": "", "total_chars": 0, "next_offset": None})

    result = _invoke(name="deck.pptx")

    assert "unsupported" in result


def test_missing_file_lists_what_is_there(rails):
    routes, seen = rails
    routes["/llama_bot/uploads"] = (200, {"files": [{"name": "leads.xlsx", "size": 10, "modified_at": "x"}]})

    result = _invoke(name="leeds.xlsx")

    assert "leads.xlsx" in result
    assert len(seen) == 2


def test_old_gem_without_the_endpoint_is_reported_plainly(rails):
    """A box whose gem predates /llama_bot/uploads 404s the index too."""
    _routes, _seen = rails
    result = _invoke(name="leads.xlsx")
    assert "leads.xlsx" in result


def test_text_is_capped_before_it_re_enters_context(rails):
    routes, _seen = rails
    routes["/llama_bot/uploads/report.pdf"] = (200, {**PAGE, "text": "a" * 100_000})

    result = _invoke()

    assert len(result) < 30_000


def test_token_is_never_echoed(rails):
    routes, _seen = rails
    routes["/llama_bot/uploads/report.pdf"] = (200, PAGE)
    assert "signed-token" not in _invoke()


# --- one-line wiring for custom modes ----------------------------------------

def test_standard_tools_is_both_shared_tools():
    tools = standard_tools()
    assert [t.name for t in tools] == ["rails_api_request", "read_uploaded_file"]
    assert tools[0] is rails_api_request
    assert tools[1] is read_uploaded_file  # user-scoped, never the box variant


def test_standard_tools_returns_a_fresh_list():
    standard_tools().append("junk")
    assert len(standard_tools()) == 2


def test_standard_prompt_sections_names_both_tools():
    text = standard_prompt_sections({})
    assert "rails_api_request" in text
    assert "read_uploaded_file" in text


def test_standard_prompt_sections_carries_the_sign_in_line_only_when_signed_out():
    assert "/users/sign_in" in standard_prompt_sections({"rails_auth": "signed_out"})
    assert "/users/sign_in" not in standard_prompt_sections({"rails_auth": "signed_in"})
    assert standard_prompt_sections(None) == standard_prompt_sections({})


# --- rails_agent -------------------------------------------------------------

def test_rails_agent_has_the_box_read_uploaded_file():
    from app.agents.leonardo.rails_agent import nodes

    tools = nodes.agent_tools()
    matches = [t for t in tools if getattr(t, "name", None) == "read_uploaded_file"]
    assert matches == [box_read_uploaded_file]


def test_rails_agent_prompt_steers_uploads_to_the_tool_even_under_an_override():
    from app.agents.leonardo.rails_agent import nodes
    from app.agents.leonardo.rails_agent.prompts import UPLOADED_FILES_DIRECTIVE

    with patch(
        "app.agents.leonardo.project_context.resolve_base_prompt",
        return_value="OVERRIDDEN BASE PROMPT",
    ):
        text = nodes.get_cached_system_prompt().content[0]["text"]

    assert UPLOADED_FILES_DIRECTIVE.strip() in text
    assert "read_uploaded_file" in UPLOADED_FILES_DIRECTIVE
