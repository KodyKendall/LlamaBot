"""Run the Node tests for LP-23 rails_auth signal.

CI only invokes node through these pytest wrappers. The
--experimental-default-type=module flag is load-bearing on the container's
node 18 (it otherwise reads the imported .js sources as CommonJS).
"""

import subprocess
from pathlib import Path


def test_rails_auth_signal_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/rails_auth_signal.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
