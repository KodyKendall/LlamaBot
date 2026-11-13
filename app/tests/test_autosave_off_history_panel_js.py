"""Run the Node tests for the LP-49 History panel (calm wording when auto-save is off)."""

import subprocess
from pathlib import Path


def test_autosave_off_history_panel_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "--experimental-default-type=module", "--test", "tests/js/autosave_off_history_panel.test.mjs"],
        cwd=app_root, capture_output=True, text=True, timeout=30, check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
