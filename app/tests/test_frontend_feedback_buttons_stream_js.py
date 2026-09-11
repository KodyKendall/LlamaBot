"""Run the Node tests for mid-stream 👍/👎 availability (0.7.9).

Without this wrapper the .mjs suite never runs in CI — the pipeline only invokes
node through these pytest shims, and the --experimental-default-type=module flag
is load-bearing on the container's node 18.
"""

import subprocess
from pathlib import Path


def test_feedback_buttons_during_stream_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/feedback_buttons_during_stream.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
