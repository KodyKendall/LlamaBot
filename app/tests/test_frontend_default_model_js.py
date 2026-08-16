"""Run the Node tests for 'the chat dropdown follows the server default model'.

The 0.7.0 release blocker: the backend default was Muse, the frontend shipped
DeepSeek, and the frontend always won because it sent an explicit llm_model.
"""

import subprocess
from pathlib import Path


def test_default_model_follows_server_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/default_model_follows_server.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
