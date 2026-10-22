"""Run the Node tests for 'a chat with discard warns and Undo stays in chat'.

Without this wrapper the .mjs suite never runs in CI (0.7.15).
"""

import subprocess
from pathlib import Path


def test_discard_warning_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/discard_warning.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
