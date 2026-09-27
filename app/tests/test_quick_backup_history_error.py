"""The Backup History error line must come from quick_backup.sh's summary, not stderr.

leo-lozeki (2026-09-24): every per-task backup showed as failed, and the error column was
the first 500 chars of stderr, i.e. aws "Skipping file ..." warnings about codex symlinks.
The line that actually says what failed is in the summary block on stdout.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.routers import slash_commands


SUMMARY_STDOUT = """Quick backup: leo-lozeki @ 20260923-101010
Step 1/3: Syncing Leonardo source code...
Step 2/3: Backing up llamapress_production...

════════════════════════════════════════
❌ Backup FAILED in 41s
════════════════════════════════════════
  Step 1 — source code:          ✅ success (13s)
  Step 2 — llamapress_production: ❌ FAILED (20s)
  Step 3 — llamabot_production:   ✅ success (8s)
════════════════════════════════════════
"""

NOISY_STDERR = "\n".join(
    f"warning: Skipping file /home/ubuntu/Leonardo/.leonardo/chatgpt-auth/1/tmp/arg0/x/link{i}. "
    "File does not exist."
    for i in range(20)
)


def _run(tmp_path, returncode, stdout, stderr):
    history = tmp_path / "backup_history.json"
    result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    with patch.object(slash_commands, "BACKUP_HISTORY_FILE", str(history)), \
         patch.object(slash_commands, "execute_command", return_value=result):
        slash_commands._run_backup_in_background("abc123")
    return json.loads(history.read_text())[-1]


def test_failed_backup_error_names_the_failed_step_not_stderr_warnings(tmp_path):
    entry = _run(tmp_path, 1, SUMMARY_STDOUT, NOISY_STDERR)

    assert entry["status"] == "failed"
    assert "Backup FAILED" in entry["error"]
    assert "llamapress_production: ❌ FAILED" in entry["error"]
    assert "Skipping file" not in entry["error"]
    assert slash_commands._backup_status["abc123"]["error"] == entry["error"]


def test_failed_backup_without_a_summary_falls_back_to_stderr(tmp_path):
    entry = _run(tmp_path, 1, "Usage: quick_backup.sh <instance_name> <s3_bucket_path>", "boom")

    assert entry["status"] == "failed"
    assert entry["error"] == "boom"


def test_failed_backup_with_no_output_says_unknown(tmp_path):
    entry = _run(tmp_path, 1, "", "")

    assert entry["error"] == "Unknown error"


def test_successful_backup_has_no_error(tmp_path):
    entry = _run(tmp_path, 0, SUMMARY_STDOUT.replace("❌ Backup FAILED", "✅ Backup complete"), NOISY_STDERR)

    assert entry["status"] == "completed"
    assert entry["error"] is None
