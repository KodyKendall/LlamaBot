"""Run the Node tests for the LP-44 image notice (both attach paths, no auto-switch)."""

import subprocess
from pathlib import Path


def test_uploaded_image_notice_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "--experimental-default-type=module", "--test", "tests/js/uploaded_image_notice.test.mjs"],
        cwd=app_root, capture_output=True, text=True, timeout=30, check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
