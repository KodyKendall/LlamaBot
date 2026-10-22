"""Run the Node tests for 'a chat with new chat shows up in history'.

Without this wrapper the .mjs suite never runs in CI (0.7.15).
"""

import subprocess
from pathlib import Path


def test_thread_list_refresh_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/thread_list_refresh.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
