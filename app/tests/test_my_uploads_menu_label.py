"""Guard: the file-attach menu's browse entry reads "My Uploads".

"Browse and attach" described the mechanics, not the thing — the menu item
opens the panel listing files this user already uploaded. Renamed 2026-08-21.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_HTML = REPO_ROOT / "app/frontend/chat.html"

BROWSE_BUTTON = re.compile(
    r'<button[^>]*data-llamabot="browse-files-btn".*?</button>',
    re.IGNORECASE | re.DOTALL,
)


def browse_button():
    match = BROWSE_BUTTON.search(CHAT_HTML.read_text())
    assert match, "chat.html no longer has a browse-files-btn menu item"
    return match.group(0)


def test_browse_menu_item_is_labelled_my_uploads():
    assert "My Uploads" in browse_button()


def test_old_browse_and_attach_label_is_gone():
    assert "Browse and attach" not in CHAT_HTML.read_text()
