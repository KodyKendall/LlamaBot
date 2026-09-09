"""An expired ChatGPT connection must not strand the customer (0.7.8).

The provider's 401 says:

    "Provided authentication token is expired. Please try signing in again."

That sentence is addressed to whoever holds the API credential. It went to the chat
verbatim, and the customer read it as their LlamaPress session. Richie (Vice Theory
Studios, paying Business) spent 62 minutes hunting for a sign-out button —
"How do I sign out???", then "I'm getting this error but I have no way of signing out
to 'sign back in again'" — and there is no such button, because signing out of
LlamaPress would not have helped. The credential that expired is the ChatGPT link.

So on a `token_expired` from the ChatGPT-subscription path the chat must:
  1. name the ChatGPT connection, NOT their LlamaPress session;
  2. point at the reconnect flow that actually exists;
  3. tell them they can switch models and keep working.

One trap worth pinning: `GET /user_instances/:id/codex` is NOT the reconnect path —
it is the v2 terminal with `codexd` auto-typed, behind `require_terminal_access!`. A
first draft of the reply to this very customer said otherwise and had to be corrected
before sending. Nothing here may point there.

Telemetry keeps the raw provider text — support triage needs the real error.
"""

import pytest

from app.websocket.error_text import (
    RECONNECT_DOC_URL,
    describe_exception,
    is_chatgpt_token_expired,
    user_facing_error,
)


class _AuthenticationError(Exception):
    """Shaped like openai.AuthenticationError as it reaches us."""


def _expired():
    return _AuthenticationError(
        "Error code: 401 - {'error': {'message': 'Provided authentication token is "
        "expired. Please try signing in again.', 'code': 'token_expired'}, "
        "'status': 401}"
    )


# --- detection ---------------------------------------------------------------


def test_recognises_the_expired_chatgpt_token():
    assert is_chatgpt_token_expired(_expired()) is True


def test_does_not_claim_unrelated_auth_failures():
    # A bad platform API key is a different problem with a different answer;
    # telling that user to reconnect ChatGPT would be a fresh dead end.
    assert is_chatgpt_token_expired(
        _AuthenticationError("Error code: 401 - Incorrect API key provided: sk-***")
    ) is False


def test_does_not_claim_ordinary_failures():
    import httpx

    assert is_chatgpt_token_expired(httpx.ReadError("")) is False
    assert is_chatgpt_token_expired(RuntimeError("boom")) is False


# --- what the customer reads -------------------------------------------------


def test_names_the_chatgpt_connection_not_their_session():
    text = user_facing_error(_expired())

    assert "ChatGPT" in text
    lowered = text.lower()
    # The exact misreading that cost 62 minutes.
    assert "sign out" not in lowered
    assert "log out" not in lowered


def test_points_at_the_reconnect_flow_that_exists():
    text = user_facing_error(_expired())

    assert RECONNECT_DOC_URL in text
    # NOT the terminal route, which is gated and is not the reconnect path.
    assert "/codex" not in text.replace(RECONNECT_DOC_URL, "")


def test_offers_a_way_to_keep_working():
    text = user_facing_error(_expired())
    lowered = text.lower()

    assert "model" in lowered, "the customer must be told they can switch models"


def test_does_not_parrot_the_provider_sentence():
    text = user_facing_error(_expired())

    assert "Please try signing in again" not in text


# --- everything else is unchanged --------------------------------------------


def test_other_errors_keep_the_existing_description():
    import httpx

    # Still the 0.7.x contract from error_text's own docstring: never a bare
    # dangling colon, always the class name.
    assert user_facing_error(httpx.ReadError("")) == describe_exception(httpx.ReadError(""))
    assert user_facing_error(RuntimeError("boom")) == "RuntimeError: boom"


def test_telemetry_still_gets_the_raw_provider_text():
    # describe_exception feeds mothership.report_error; support triage needs the
    # real 401, not our friendlier rewrite.
    raw = describe_exception(_expired())

    assert "token_expired" in raw
    assert RECONNECT_DOC_URL not in raw


# --- the error frame the browser actually receives ---------------------------


def test_the_prefix_is_dropped_for_the_chatgpt_case():
    from app.websocket.error_text import chat_error_content

    content = chat_error_content("Error processing request", _expired())

    # "Error processing request:" in front of an explanation and two numbered
    # options reads as a crash report — the register that made the original
    # message unactionable.
    assert not content.startswith("Error processing request")
    assert "ChatGPT connection has expired" in content


def test_the_prefix_is_kept_for_everything_else():
    import httpx

    from app.websocket.error_text import chat_error_content

    assert chat_error_content("Error processing request", httpx.ReadError("")) == (
        "Error processing request: ReadError"
    )


def test_every_chat_facing_error_frame_routes_through_the_helper():
    """Three sites send an error frame to the browser (mid-stream, after an
    approval, after a question). A new one that formats its own string would
    reintroduce the raw 401 silently, so assert the shape at the source."""
    from pathlib import Path

    handler = Path(__file__).resolve().parents[1] / "websocket" / "request_handler.py"
    source = handler.read_text()

    for line in source.splitlines():
        if '"content": f"Error' in line:
            raise AssertionError(
                f"chat error frame built by hand instead of chat_error_content: {line.strip()}"
            )
    assert source.count("chat_error_content(") >= 3
