"""Run the Node half of the mid-run restart reproduction (see test_restart_midrun.py)."""

import subprocess
from pathlib import Path


def test_restart_midrun_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/restart_midrun_reconnect.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
