"""Starting the receiver at login, and knowing whether anything is receiving.

Each platform is exercised by switching `_platform` and pointing the home
directory at a temp dir, so the macOS and Linux paths run on any machine
and nothing here touches the real registry, LaunchAgents or systemd.
"""

from __future__ import annotations

import ast
import plistlib
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
    assert agent["ProgramArguments"][-3:] == ["flanner", "peer", "serve"]
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
    assert "peer serve" in unit and "WantedBy=default.target" in unit
    assert f"FLANNER_HOME={autostart._home()}" in unit
    assert ("systemctl", "--user", "enable", "--now", f"{autostart.name()}.service") in home
    assert autostart.disable() is True
    assert not Path(where).exists()


def test_the_windows_login_command_is_valid_python_for_this_home():
    command = autostart._windows_command()
    script = command.split(' -c "', 1)[1].rstrip('"')
    ast.parse(script)
    assert repr(str(autostart._home())) in script
    assert "'peer','start'" in script


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
