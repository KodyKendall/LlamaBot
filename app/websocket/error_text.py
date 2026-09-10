"""Human-readable text for an exception we are about to show a user.

Why this exists (incident accf39ac3aa6, leo-puzero, rails_engineer_plan_mode_agent):
a provider dropped the socket mid-response while a graph was being resumed after
an ``ask_user_question`` answer. httpcore raised ``ReadError``, httpx mapped it to
``httpx.ReadError`` — and **that exception carries no message at all**:

    >>> str(httpx.ReadError(""))
    ''

Every user-facing error frame was built as ``f"Error resuming after question:
{str(e)}"``, so what actually reached the browser was::

    Error resuming after question:

A dead end for the user *and* for support: the report arrives with a blank cause,
and the exception type — the only information this class of failure carries — was
thrown away at exactly the moment it mattered. ``httpx`` is not unusual here;
message-less exceptions are common in the transport layer (``ReadError``,
``WriteError``, ``ConnectError``, bare ``CancelledError``), which is precisely the
layer that fails during a long streamed model call.

So: always keep the class name, and never emit a bare dangling colon.

Pinned by ``app/tests/test_midstream_read_error.py``.
"""


def describe_exception(exc: BaseException) -> str:
    """Return ``"ClassName: message"``, or just ``"ClassName"`` when there is none.

    The class name is always included, even when a message exists: "ReadError"
    and "PoolTimeout" tell a reader (and a support triage queue) more than most
    provider messages do, and it keeps every error frame self-identifying.
    """
    name = type(exc).__name__
    message = str(exc).strip()
    return f"{name}: {message}" if message else name


# --- The customer-facing rewrite for an expired ChatGPT connection -----------
#
# Incident agent_task 323 (InstanceError 5729/5716, instance 2461, box leo-zuset,
# 2026-09-09). The provider's 401 says:
#
#     "Provided authentication token is expired. Please try signing in again."
#
# That sentence is addressed to whoever holds the API credential. It went to the
# chat verbatim, and the customer — Richie, a paying Business customer — read it as
# his LlamaPress session. He sent six messages over 34 minutes with no answer to
# any of them; the fifth was "How do I sign out???". Then he emailed: "I'm getting
# this error but I have no way of signing out to 'sign back in again'." There is no
# such button, and signing out of LlamaPress would not have helped: the credential
# that expired is the ChatGPT link on the box.
#
# 62 minutes blocked, on a path carrying 3,624 messages across 12 boxes in the week
# it was filed, 7 of them paying customers.

#: The documented reconnect flow. NOT ``/user_instances/:id/codex`` — that is the
#: v2 terminal with ``codexd`` auto-typed, behind ``require_terminal_access!``. A
#: first draft of the reply to this customer pointed there and had to be corrected
#: before it was sent; nothing user-facing may repeat that.
RECONNECT_DOC_URL = "https://llamapress.ai/wiki/use-your-chatgpt-plan-with-leo"

_CHATGPT_EXPIRED_MESSAGE = (
    "Your ChatGPT connection has expired — this is the ChatGPT account you linked "
    "to Leo, not your LlamaPress account, so there is nothing to do on this side. "
    "Two ways forward:\n\n"
    "1. Reconnect ChatGPT: open the model picker, choose a \"my ChatGPT plan\" "
    f"model and press Connect ChatGPT. Step-by-step: {RECONNECT_DOC_URL}\n"
    "2. Or switch to any other model in the picker and carry on right now — those "
    "run on our own keys and need no connection of yours.\n\n"
    "Your conversation is safe either way; nothing was lost."
)


def is_chatgpt_token_expired(exc: BaseException) -> bool:
    """Whether this is a ChatGPT-subscription credential that has aged out.

    Matched on ``token_expired``, the provider's own error code, rather than on
    the prose or the status: a 401 from a bad platform API key is a different
    problem with a different answer ("Incorrect API key provided"), and telling
    that user to reconnect ChatGPT would be a fresh dead end.
    """
    return "token_expired" in str(exc)


def user_facing_error(exc: BaseException) -> str:
    """The text to put in the CHAT for this exception.

    Differs from ``describe_exception`` in exactly one case today. Telemetry keeps
    using ``describe_exception``: support triage needs the real provider 401, not
    our friendlier rewrite of it.
    """
    if is_chatgpt_token_expired(exc):
        return _CHATGPT_EXPIRED_MESSAGE
    return describe_exception(exc)


def chat_error_content(prefix: str, exc: BaseException) -> str:
    """The ``content`` for an error frame headed to the browser.

    Keeps the existing ``"<prefix>: <ClassName: message>"`` shape for everything
    ordinary, but drops the prefix for the ChatGPT-expiry case: "Error processing
    request:" in front of an explanation and two numbered options reads as a crash
    report, which is the register that made the original message unactionable.
    """
    if is_chatgpt_token_expired(exc):
        return _CHATGPT_EXPIRED_MESSAGE
    return f"{prefix}: {describe_exception(exc)}"
