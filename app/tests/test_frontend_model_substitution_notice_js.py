"""Run the Node tests for the model-swap banner.

The .mjs suite has existed since 0.7.0 but never had one of these shims, so it
has never run in CI — the pipeline only invokes node through these pytest
wrappers. Added with the 0.7.5 change that gives the frame a `reason`, which is
exactly the kind of signature change the suite is there to catch.

The --experimental-default-type=module flag is load-bearing on the container's
node 18 (it otherwise reads the imported .js sources as CommonJS).
"""

import subprocess
from pathlib import Path


def test_model_substitution_notice_node_suite():
    app_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-default-type=module",
            "--test",
            "tests/js/model_substitution_notice.test.mjs",
        ],
        cwd=app_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
