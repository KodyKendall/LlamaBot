"""A CORS preflight on a login entry point must not 400.

Mothership incident, leo-nefe 2026-08-06 (Rich Rayl, paying, live on a call): the browser
sent `OPTIONS /auth/consume?token=...` before following the login URL, LlamaBot answered
400, no GET ever followed, and the one-time grant expired unused. He burned 7 grants in an
afternoon. The user saw nothing and neither did the logs.

Cause: CORSMiddleware is configured same-origin-only (allow_origins=[]), which is correct,
so it does not answer the preflight; the request falls through to a router where only GET
is declared, and FastAPI rejects the method. The same shape ate logins on the legacy HMAC
`/login` path too, for a long time.

The mothership shipped the primary fix (no cross-origin redirect at all). This is the
belt-and-braces half: a preflight that still happens — a bookmark, an embed, another tool
opening the link — must not hard-fail.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


PREFLIGHT_HEADERS = {
    "Origin": "https://llamapress.ai",
    "Access-Control-Request-Method": "GET",
}


@pytest.mark.parametrize("path", ["/auth/consume", "/login"])
def test_preflight_is_answered_not_rejected(client, path):
    response = client.options(path, headers=PREFLIGHT_HEADERS)

    assert response.status_code == 204, (
        f"OPTIONS {path} returned {response.status_code}; a browser treats a failed "
        "preflight as a hard failure and abandons the login with no error and no log line."
    )


@pytest.mark.parametrize("path", ["/auth/consume", "/login"])
def test_preflight_echoes_the_requesting_origin(client, path):
    response = client.options(path, headers=PREFLIGHT_HEADERS)

    assert response.headers.get("access-control-allow-origin") == "https://llamapress.ai"
    assert response.headers.get("access-control-allow-credentials") == "true"
    assert "GET" in response.headers.get("access-control-allow-methods", "")


@pytest.mark.parametrize("path", ["/auth/consume", "/login"])
def test_preflight_grants_nothing_on_its_own(client, path):
    """The preflight answer must stay a preflight answer.

    It carries no data and performs no login; every real check still happens on the GET.
    A 204 that also set a session cookie would turn this belt-and-braces fix into an
    authentication bypass.
    """
    response = client.options(path, headers=PREFLIGHT_HEADERS)

    assert response.status_code == 204
    assert not response.content
    assert "set-cookie" not in {k.lower() for k in response.headers}


@pytest.mark.parametrize("path", ["/auth/consume", "/login"])
def test_get_still_rejects_a_bad_token(client, path, monkeypatch):
    """Answering OPTIONS must not loosen the GET.

    The secret is set explicitly: without it, GET /login short-circuits to 503
    ("magic-link sign-in not configured") before the token is ever verified, which
    passes on a configured dev box and fails in CI without testing anything.
    """
    monkeypatch.setenv("LLAMAPRESS_AI_LOGIN_SECRET", "test-secret-not-a-real-one")

    response = client.get(f"{path}?token=not-a-real-token", follow_redirects=False)

    assert response.status_code != 204
    assert response.status_code < 500
