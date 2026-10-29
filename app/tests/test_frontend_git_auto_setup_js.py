"""Run the Node tests for LP-20's push sign-in retry.

CI only invokes node through these pytest wrappers. The
--experimental-default-type=module flag is load-bearing on the container's
node 18 (it otherwise reads the imported .js sources as CommonJS).
"""

import subprocess
from pathlib import Path


def test_git_auto_setup_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/git_auto_setup.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
