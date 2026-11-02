"""Run the dependency-free Node tests for LP-10: Inbox/Activity tabs reload after chat SSO."""

import subprocess
from pathlib import Path


def test_sso_reloads_inbox_activity_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/sso_reloads_inbox_activity.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
