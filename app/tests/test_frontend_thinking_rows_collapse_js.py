"""Run the Node tests for 'a finished turn keeps one thinking row'.

Without this wrapper the .mjs suite never runs in CI (0.7.14).
"""

import subprocess
from pathlib import Path


def test_thinking_rows_collapse_on_end_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/thinking_rows_collapse_on_end.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
