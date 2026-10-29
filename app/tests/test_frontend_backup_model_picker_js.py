"""Run the Node tests for the LP-21 backup model picker and sticky banner.

CI only invokes node through these pytest wrappers, so the .mjs suite is
invisible without this one. The --experimental-default-type=module flag is
load-bearing on the container's node 18 (it otherwise reads the imported .js
sources as CommonJS).
"""

import subprocess
from pathlib import Path


def test_backup_model_picker_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/backup_model_picker.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
