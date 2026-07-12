"""Run the dependency-free Node tests for delegation progress UI behavior."""

import subprocess
from pathlib import Path


def test_delegation_stall_ui_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/delegation_stall_ui.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
