"""Starting the receiver at login, and knowing whether anything is receiving.

Each platform is exercised by switching `_platform` and pointing the home
directory at a temp dir, so the macOS and Linux paths run on any machine
and nothing here touches the real registry, LaunchAgents or systemd.
"""

from __future__ import annotations

import ast
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flanner import autostart


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    ran: list[tuple[str, ...]] = []
    monkeypatch.setattr(autostart, "_run", lambda *argv: ran.append(argv) or True)
    return ran


def test_each_home_gets_its_own_name(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "one"))
    first = autostart.name()
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "two"))
    assert autostart.name() != first


def test_macos_registers_a_launch_agent_for_this_home(home, monkeypatch):
    monkeypatch.setattr(autostart, "_platform", lambda: "darwin")

    where = autostart.enable()

    agent = plistlib.loads(Path(where).read_bytes())
    assert agent["ProgramArguments"][1:] == ["-c", autostart._MAIN, "peer", "serve"]
    assert agent["EnvironmentVariables"]["FLANNER_HOME"] == str(autostart._home())
    assert agent["RunAtLoad"] is True
    assert autostart.enabled()
    assert ("launchctl", "load", "-w", where) in home

    assert autostart.disable() is True
    assert not autostart.enabled()
    assert autostart.disable() is False


def test_linux_registers_a_user_service_and_starts_it(home, monkeypatch):
    monkeypatch.setattr(autostart, "_platform", lambda: "linux")

    where = autostart.enable()

    unit = Path(where).read_text(encoding="utf-8")
    assert f'-c "{autostart._MAIN}" peer serve' in unit and "WantedBy=default.target" in unit
    assert f"FLANNER_HOME={autostart._home()}" in unit
    assert ("systemctl", "--user", "enable", "--now", f"{autostart.name()}.service") in home
    assert autostart.disable() is True
    assert not Path(where).exists()


def test_macos_and_linux_start_the_package_whatever_folder_they_start_in(tmp_path, monkeypatch):
    """A folder named `flanner` where the receiver starts is not what it imports.

    `whoami`, not `--version`: `--version` answers before the command group
    runs, and the group's first import is the one that failed.
    """
    (tmp_path / "flanner").mkdir()
    monkeypatch.chdir(tmp_path)
    shown = subprocess.run(  # noqa: S603 - our own interpreter
        [sys.executable, "-c", autostart._MAIN, "whoami"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert "not signed in" in shown.stdout


def test_the_windows_login_command_is_valid_python_for_this_home():
    command = autostart._windows_command()
    script = command.split(' -c "', 1)[1].rstrip('"')
    ast.parse(script)
    assert repr(str(autostart._home())) in script
    assert "'peer','start'" in script


def test_windows_registers_the_run_entry_and_starts_receiving_now(home, monkeypatch):
    """macOS and Linux start the receiver as they register it; so does Windows."""
    monkeypatch.setattr(autostart, "_platform", lambda: "win32")
    written: list[str] = []
    launched: list[str] = []
    monkeypatch.setattr(autostart, "_registry_set", written.append)
    monkeypatch.setattr(autostart, "_launch_now", launched.append)

    autostart.enable()

    assert written == launched == [autostart._windows_command()]


def test_windows_leaves_a_running_receiver_alone(home, monkeypatch):
    monkeypatch.setattr(autostart, "_platform", lambda: "win32")
    launched: list[str] = []
    monkeypatch.setattr(autostart, "_registry_set", lambda command: None)
    monkeypatch.setattr(autostart, "_launch_now", launched.append)
    autostart.beat()

    autostart.enable()

    assert launched == []


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_login_service_that_will_not_start_is_reported_and_removed(home, monkeypatch, platform):
    monkeypatch.setattr(autostart, "_platform", lambda: platform)
    monkeypatch.setattr(autostart, "_run", lambda *argv: False)

    with pytest.raises(autostart.AutostartError, match="peer start"):
        autostart.enable()

    assert not autostart.enabled(), "a failed registration must not look like one"


def test_the_command_says_when_autostart_failed(monkeypatch):
    from click.testing import CliRunner

    from flanner.cli import cli

    def fails() -> str:
        raise autostart.AutostartError("systemctl --user could not start the service.")

    monkeypatch.setattr(autostart, "enable", fails)
    result = CliRunner().invoke(cli, ["peer", "autostart", "on"])

    assert result.exit_code == 1
    assert "could not start the service" in result.output
    assert "receives messages now" not in result.output


def test_turning_it_on_forgets_an_earlier_no(home, monkeypatch):
    monkeypatch.setattr(autostart, "_platform", lambda: "linux")
    autostart.decline()
    assert autostart.declined()

    autostart.enable()

    assert not autostart.declined()


def test_receiving_means_a_heartbeat_in_the_last_90_seconds():
    assert autostart.receiving() is False
    autostart.beat()
    assert autostart.receiving() is True
    assert autostart.receiving(now=time.time() + 91) is False
