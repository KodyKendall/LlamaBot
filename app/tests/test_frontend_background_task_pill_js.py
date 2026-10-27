"""Run the dependency-free Node tests for the background research status pill."""

import subprocess
from pathlib import Path


def test_background_task_pill_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/background_task_pill.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
