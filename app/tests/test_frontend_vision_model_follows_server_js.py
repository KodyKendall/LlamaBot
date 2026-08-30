"""Run the Node tests for the box-resolved image auto-switch target (0.7.5).

Without this wrapper the .mjs suite never runs in CI — the pipeline only invokes
node through these pytest shims, and the --experimental-default-type=module flag
is load-bearing on the container's node 18 (it otherwise reads the imported .js
sources as CommonJS and every named import fails).
"""

import subprocess
from pathlib import Path


def test_vision_model_follows_server_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/vision_model_follows_server.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
