"""Run the Node tests for the LP-47 Versions section of the History panel."""

import subprocess
from pathlib import Path


def test_app_versions_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "--experimental-default-type=module", "--test", "tests/js/app_versions.test.mjs"],
        cwd=app_root, capture_output=True, text=True, timeout=30, check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
