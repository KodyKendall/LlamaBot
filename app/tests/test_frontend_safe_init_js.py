"""Init-step guards for the chat app (leo-tama's dead chat panel, 0.7.0).

The Node suite covers the helpers; the call-site assertions below are the part
that actually regressed on `leo-tama` — a raw `?.getSelectedElements()` and an
unguarded init sequence in index.js.
"""

import subprocess
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
INDEX_JS = APP_ROOT / "frontend" / "chat" / "index.js"


def test_safe_init_node_suite():
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/safe_init.test.mjs",
        ],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_send_does_not_call_get_selected_elements_unguarded():
    """`?.` guards a null selector, not a selector missing the method."""
    source = INDEX_JS.read_text()
    assert "this.elementSelector?.getSelectedElements()" not in source, (
        "sendMessage still calls getSelectedElements() directly — a module-skewed "
        "ElementSelector makes every send throw"
    )
    assert "selectedElementsOf(this.elementSelector)" in source


def test_init_components_wraps_its_optional_steps():
    """One broken component must not take the rest of init down with it."""
    source = INDEX_JS.read_text()
    assert "safeInit(" in source, "initComponents runs its steps unguarded"
    # The step that actually blew up on leo-tama.
    assert "initAssetModal" in source
    start = source.index("initComponents()")
    end = source.index("initAssetModal")
    assert "safeInit(" in source[start:end], (
        "the file-attachment wiring is not inside a guarded init step"
    )
