"""Run the Node tests for the paywall card's "Use your ChatGPT account" action (0.7.11)."""

import subprocess
from pathlib import Path


def test_paywall_chatgpt_way_out_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/paywall_chatgpt_way_out.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
