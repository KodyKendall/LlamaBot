"""Run the dependency-free Node tests for the /cookbook slash-menu search."""

import subprocess
from pathlib import Path


def test_cookbook_slash_command_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/cookbook_slash_command.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
