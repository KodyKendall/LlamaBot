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
