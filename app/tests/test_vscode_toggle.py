"""Tests for the `enable_vscode` site setting.

One setting owns two things: whether the Code tab appears in the chat browser
pane, and whether the code-server ("code") container runs. Keeping both under a
single key is deliberate — two independent switches can disagree, and the way
they disagree is a visible Code tab pointing at a stopped editor.

The setting defaults to "false", so a box boots with no editor and no Code tab
until someone turns the editor on in Settings.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _fake_run(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------- the setting


class TestSettingKey:
    def test_enable_vscode_is_a_writable_site_setting(self):
        from app.routers.api import VALID_SITE_SETTINGS

        assert "enable_vscode" in VALID_SITE_SETTINGS

    def test_helper_defaults_to_disabled(self):
        from app.services import vscode_service

        with patch("app.routers.api.get_site_setting", return_value="false") as gss:
            assert vscode_service.vscode_enabled(MagicMock()) is False
            assert gss.call_args.args[1] == "enable_vscode"
            assert gss.call_args.args[2] == "false"

    def test_helper_true_only_for_the_string_true(self):
        from app.services import vscode_service

        with patch("app.routers.api.get_site_setting", return_value="true"):
            assert vscode_service.vscode_enabled(MagicMock()) is True

    def test_helper_fails_closed_when_the_setting_read_raises(self):
        from app.services import vscode_service

        with patch("app.routers.api.get_site_setting", side_effect=RuntimeError("no db")):
            assert vscode_service.vscode_enabled(MagicMock()) is False


# ------------------------------------------------------------------- the tab


class TestCodeTabVisibility:
    """The Code tab follows `enable_vscode`, never `visible_tabs`.

    `visible_tabs` still owns the other optional tabs, and its "never set"
    default still means "show them all". The Code tab is excluded from that
    catalog so an old stored value can't switch the editor tab back on.
    """

    def _resolve(self, *, vscode="false", tabs=None):
        from app.routers import ui

        def fake_setting(session, key, default=None):
            if key == "enable_vscode":
                return vscode
            if key == "visible_tabs":
                return tabs
            return default

        with patch("app.routers.api.get_site_setting", side_effect=fake_setting):
            return ui.resolve_visible_tabs(MagicMock())

    def test_code_tab_hidden_by_default(self):
        assert "vsCodeFrame" not in self._resolve()

    def test_code_tab_shown_when_editor_enabled(self):
        assert "vsCodeFrame" in self._resolve(vscode="true")

    def test_stored_visible_tabs_cannot_turn_the_code_tab_on(self):
        tabs = "vsCodeFrame,inboxFrame"
        resolved = self._resolve(vscode="false", tabs=tabs)
        assert "vsCodeFrame" not in resolved
        assert "inboxFrame" in resolved

    def test_other_tabs_still_default_to_visible(self):
        resolved = self._resolve()
        for target in ("inboxFrame", "activityFrame"):
            assert target in resolved

    def test_app_tab_always_present(self):
        assert "liveSiteFrame" in self._resolve()

    def test_code_tab_is_not_in_the_visible_tabs_catalog(self):
        from app.routers.ui import OPTIONAL_BROWSER_TABS

        assert "vsCodeFrame" not in [t["target"] for t in OPTIONAL_BROWSER_TABS]


# -------------------------------------------------------------- the container


class TestContainerControl:
    def test_start_uses_the_compose_profile(self):
        from app.services import vscode_service

        with patch.object(vscode_service, "_compose_files", return_value=[]), \
             patch.object(vscode_service, "run_host_command", return_value=_fake_run()) as run:
            result = vscode_service.start_vscode()

        command = run.call_args.args[0]
        assert "docker compose" in command
        assert "--profile code" in command
        assert "up -d code" in command
        assert result["ok"] is True

    def test_stop_uses_the_compose_profile(self):
        from app.services import vscode_service

        with patch.object(vscode_service, "_compose_files", return_value=[]), \
             patch.object(vscode_service, "run_host_command", return_value=_fake_run()) as run:
            result = vscode_service.stop_vscode()

        command = run.call_args.args[0]
        assert "--profile code" in command
        assert "stop code" in command
        assert result["ok"] is True

    def test_compose_file_override_is_honored(self):
        """The dev box runs docker-compose-dev.yml, not the default file."""
        from app.services import vscode_service

        with patch.dict("os.environ", {"LEONARDO_COMPOSE_FILE": "docker-compose-dev.yml"}), \
             patch.object(vscode_service, "run_host_command", return_value=_fake_run()) as run:
            vscode_service.start_vscode()

        assert "-f docker-compose-dev.yml" in run.call_args.args[0]

    def test_two_override_files_become_two_flags(self):
        from app.services import vscode_service

        with patch.dict("os.environ", {"LEONARDO_COMPOSE_FILE": "base.yml,override.yml"}), \
             patch.object(vscode_service, "run_host_command", return_value=_fake_run()) as run:
            vscode_service.start_vscode()

        command = run.call_args.args[0]
        assert "-f base.yml -f override.yml" in command

    def test_compose_file_is_read_off_the_existing_container(self):
        """Docker records which compose file created a container. Trust it."""
        from app.services import vscode_service

        label = _fake_run(stdout="/home/ubuntu/dev/Leonardo/docker-compose-dev.yml\n")
        with patch.dict("os.environ", {"LEONARDO_COMPOSE_FILE": ""}), \
             patch.object(vscode_service, "run_host_command", return_value=label):
            files = vscode_service._compose_files()

        assert files == ["/home/ubuntu/dev/Leonardo/docker-compose-dev.yml"]

    def test_no_container_means_the_default_compose_file(self):
        from app.services import vscode_service

        with patch.dict("os.environ", {"LEONARDO_COMPOSE_FILE": ""}), \
             patch.object(vscode_service, "run_host_command",
                          return_value=_fake_run(returncode=1, stderr="No such object: code")):
            assert vscode_service._compose_files() == []

    def test_unlabeled_container_means_the_default_compose_file(self):
        from app.services import vscode_service

        with patch.dict("os.environ", {"LEONARDO_COMPOSE_FILE": ""}), \
             patch.object(vscode_service, "run_host_command", return_value=_fake_run(stdout="<no value>\n")):
            assert vscode_service._compose_files() == []

    def test_failure_is_reported_not_raised(self):
        from app.services import vscode_service

        failed = _fake_run(returncode=1, stderr="no such service: code")
        with patch.object(vscode_service, "_compose_files", return_value=[]), \
             patch.object(vscode_service, "run_host_command", return_value=failed):
            result = vscode_service.start_vscode()

        assert result["ok"] is False
        assert "no such service" in result["output"]

    def test_command_timeout_is_reported_not_raised(self):
        from app.services import vscode_service

        with patch.object(vscode_service, "_compose_files", return_value=[]), \
             patch.object(vscode_service, "run_host_command", side_effect=OSError("boom")):
            result = vscode_service.start_vscode()

        assert result["ok"] is False

    def test_running_reads_docker_inspect(self):
        from app.services import vscode_service

        with patch.object(vscode_service, "run_host_command", return_value=_fake_run(stdout="true\n")) as run:
            assert vscode_service.vscode_running() is True
        assert "docker inspect" in run.call_args.args[0]

    def test_missing_container_counts_as_not_running(self):
        from app.services import vscode_service

        missing = _fake_run(returncode=1, stderr="No such object: code")
        with patch.object(vscode_service, "run_host_command", return_value=missing):
            assert vscode_service.vscode_running() is False


# ------------------------------------------------------------- boot reconcile


class TestStartupReconcile:
    """A reboot must not bring the editor back.

    The compose profile is the primary guard, but an existing box may still have
    a compose file without the profile. The boot check is the second guard, and
    it is the one that ships inside LlamaBot.
    """

    @pytest.mark.asyncio
    async def test_stops_the_container_when_the_setting_is_off(self):
        from app.services import vscode_service

        with patch.object(vscode_service, "vscode_enabled", return_value=False), \
             patch.object(vscode_service, "vscode_running", return_value=True), \
             patch.object(vscode_service, "stop_vscode", return_value={"ok": True, "output": ""}) as stop:
            action = await vscode_service.reconcile_vscode(MagicMock())

        stop.assert_called_once()
        assert action == "stopped"

    @pytest.mark.asyncio
    async def test_leaves_a_running_container_alone_when_enabled(self):
        from app.services import vscode_service

        with patch.object(vscode_service, "vscode_enabled", return_value=True), \
             patch.object(vscode_service, "vscode_running", return_value=True), \
             patch.object(vscode_service, "stop_vscode") as stop:
            action = await vscode_service.reconcile_vscode(MagicMock())

        stop.assert_not_called()
        assert action == "none"

    @pytest.mark.asyncio
    async def test_does_not_start_the_container_on_boot(self):
        """Enabling is a human action. Boot never starts the editor by itself."""
        from app.services import vscode_service

        with patch.object(vscode_service, "vscode_enabled", return_value=True), \
             patch.object(vscode_service, "vscode_running", return_value=False), \
             patch.object(vscode_service, "start_vscode") as start:
            action = await vscode_service.reconcile_vscode(MagicMock())

        start.assert_not_called()
        assert action == "none"

    @pytest.mark.asyncio
    async def test_never_raises_into_startup(self):
        from app.services import vscode_service

        with patch.object(vscode_service, "vscode_enabled", side_effect=RuntimeError("no db")):
            action = await vscode_service.reconcile_vscode(MagicMock())

        assert action == "error"
