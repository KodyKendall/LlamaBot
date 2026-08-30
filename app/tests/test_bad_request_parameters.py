"""A rejected model request must say what we actually sent (0.7.5, P1-3).

Fleet telemetry, 7 days: 33 BadRequestError rows, which the 2026-08-25 follow-up split into
three genuinely different conditions that had been counted as one:

  * 15 (9 boxes)  "The request contains invalid parameters. Check the request body..."
                  — the real P1-3. The provider names no `param`, so the message alone is
                  useless and we log nothing about the request we sent.
  * 8  (3 boxes)  "This model's maximum context length is 1048576 tokens..." — the 1M
                  ceiling, not a bad request at all.
  * 10            "`messages` must contain at least one message with role `user` or `tool`"
                  — malformed history, which is P1-4's class and already handled.

0.7.4 added log_bad_request_shape(), but it describes the MESSAGES. "Invalid parameters" is
about the request PARAMETERS — temperature, max_tokens, tool_choice, response_format — and
those were never recorded. This adds them, redacted, and classifies the three shapes so the
mothership stops averaging three different bugs together.
"""
import pytest

from app.agents.leonardo.message_invariants import (
    classify_bad_request,
    redact_request_params,
)


class _Boom(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.status_code = 400


INVALID_PARAMS = "The request contains invalid parameters. Check the request body and try again."
CONTEXT_LEN = (
    "This model's maximum context length is 1048576 tokens. However, you requested 1048669 tokens."
)
BAD_HISTORY = "`messages` must contain at least one message with role `user` or `tool`"


def test_invalid_parameters_is_its_own_class():
    assert classify_bad_request(_Boom(INVALID_PARAMS)) == "invalid_parameters"


def test_context_length_is_not_reported_as_a_bad_request():
    """It is the 1M ceiling. Counting it as P1-3 is what made P1-3 look bigger than it is."""
    assert classify_bad_request(_Boom(CONTEXT_LEN)) == "context_length_exceeded"


def test_malformed_history_is_attributed_to_the_repair_path():
    assert classify_bad_request(_Boom(BAD_HISTORY)) == "malformed_history"


def test_an_unrecognised_400_is_labelled_unknown_not_guessed():
    assert classify_bad_request(_Boom("something new from a provider")) == "unknown"


def test_request_params_are_recorded_because_the_provider_names_none():
    params = redact_request_params({
        "temperature": 0.7,
        "max_tokens": 4096,
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
        "top_p": 1,
    })

    assert params["temperature"] == 0.7
    assert params["max_tokens"] == 4096
    assert params["tool_choice"] == "auto"
    assert params["response_format"] == {"type": "json_object"}


def test_credentials_never_reach_the_log():
    """This payload goes to the mothership; a key in it would be a disclosure, not a clue."""
    params = redact_request_params({
        "api_key": "sk-live-abcdef123456",
        "temperature": 0.2,
        "default_headers": {"Authorization": "Bearer sk-live-abcdef123456"},
        "base_url": "https://api.provider.example/v1",
    })

    flat = repr(params)
    assert "sk-live-abcdef123456" not in flat
    assert params["temperature"] == 0.2
    # The base_url is diagnostic (it says WHICH gateway rejected us) and carries no secret.
    assert params["base_url"] == "https://api.provider.example/v1"


def test_message_bodies_are_not_copied_into_the_params_blob():
    """The message shape is logged separately; duplicating content here risks user data."""
    params = redact_request_params({
        "messages": [{"role": "user", "content": "my private business plan"}],
        "temperature": 0.1,
    })

    assert "my private business plan" not in repr(params)


def test_unknown_keys_are_kept_but_values_are_bounded():
    """A future provider parameter should still show up — that is the whole point."""
    params = redact_request_params({"some_new_flag": "x" * 5000})

    assert "some_new_flag" in params
    assert len(str(params["some_new_flag"])) <= 512
