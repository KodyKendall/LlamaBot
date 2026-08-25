"""Shapes Rails crash-feed entries into what the chat page's error tray renders.

The tray (``app/frontend/chat/ui/ErrorAttach.js``) is the quiet one-line notice
above the composer. It already carries JavaScript errors the preview pushes over
postMessage; this is the server-side half — the same button press that renders a
500 puts a line there too, so the user is never left looking at a Rails error
page wondering whether Leo can see it.

Deliberately separate from ``app/lib/rails_error_watch``, which formats the SAME
feed entries for the model. The two want opposite things: the model gets prose,
a "fix this now" instruction and 12 backtrace frames; the tray gets short fields
a popup can lay out and a person can skim. Sharing a formatter between them
would make both worse.

The entries are app-controlled text, so everything here is defensive: an entry
with nothing usable in it is dropped rather than rendered blank, the backtrace
is trimmed, and the list is capped. The browser escapes it on render; this only
has to keep the payload sane.

See docs/dev/rails_auto_recovery.md.
"""
from typing import Any, Dict, List, Optional

# The tray is a notice, not a log viewer. A crash loop that raises forty
# distinct errors is still one "something is broken" for the user.
MAX_TRAY_ERRORS = 10

# Enough to name the file and the line the user needs, not the framework.
MAX_BACKTRACE_LINES = 8

MAX_MESSAGE_CHARS = 500


def tray_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """One feed entry as a tray row, or None if it carries nothing to show."""
    if not isinstance(entry, dict):
        return None

    error_class = str(entry.get("error_class") or "").strip()
    message = str(entry.get("message") or "").strip()[:MAX_MESSAGE_CHARS]

    if not error_class and not message:
        return None

    # ``NoMethodError: undefined method `title' for nil``, or either half alone.
    text = ": ".join(part for part in (error_class, message) if part)

    return {
        "id": f"rails-{entry.get('seq')}",
        "kind": "rails",
        "message": text,
        # One field, because the tray dedupes on it and renders it as "on X".
        "path": _where(entry),
        "count": _count(entry),
        "stack": _stack(entry),
    }


def tray_entries(entries: Any) -> List[Dict[str, Any]]:
    """Shape a feed page, newest-biased and capped."""
    if not isinstance(entries, list):
        return []

    shaped = [row for row in (tray_entry(e) for e in entries) if row is not None]

    # The feed is oldest-first, so when there are too many the tail is the part
    # that just happened — which is the part the user is looking at.
    return shaped[-MAX_TRAY_ERRORS:]


def _where(entry: Dict[str, Any]) -> str:
    method = str(entry.get("method") or "").strip()
    path = str(entry.get("path") or "").strip()
    if not path:
        # A background job crashes with no request. The tray drops the "on …"
        # line entirely rather than showing a method with nowhere to point.
        return ""
    return f"{method} {path}".strip()


def _count(entry: Dict[str, Any]) -> int:
    try:
        count = int(entry.get("count") or 1)
    except (TypeError, ValueError):
        return 1
    return count if count > 0 else 1


def _stack(entry: Dict[str, Any]) -> Optional[str]:
    backtrace = entry.get("backtrace")
    if not isinstance(backtrace, list) or not backtrace:
        return None
    frames = [str(frame) for frame in backtrace[:MAX_BACKTRACE_LINES]]
    return "\n".join(frames) or None
