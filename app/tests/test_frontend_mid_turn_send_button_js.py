"""Run the dependency-free Node tests for the send/stop button while Leo is working."""

import subprocess
from pathlib import Path


def test_mid_turn_send_button_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/mid_turn_send_button.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
