"""The launchers the desktop app points at the flanner it bundles.

FLANNER_HOME is a temp dir for every test (conftest), so these write
launchers there and never into the developer's own ~/.flanner/bin.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from flanner import desktop
from flanner.cli import cli

pytest.importorskip("pip._vendor.distlib.scripts", reason="launchers are written with pip")

REPO = Path(__file__).resolve().parent.parent


def _env() -> dict[str, str]:
    """Run this checkout's flanner through the launchers, not an installed one."""
    return {**os.environ, "PYTHONPATH": str(REPO)}


def _launcher(name: str) -> Path:
    return desktop.bin_dir() / (f"{name}.exe" if sys.platform == "win32" else name)


def test_link_writes_both_launchers_and_they_run_this_python():
    made = desktop.link()

    assert sorted(made) == sorted([_launcher("flanner"), _launcher("flanner-mcp")])
    shown = subprocess.run(  # noqa: S603 - a launcher this test just wrote
        [str(_launcher("flanner")), "--version"],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=120,
    )
    assert shown.returncode == 0, shown.stderr
    assert "flanner, version" in shown.stdout


def test_linking_again_while_an_agent_runs_a_launcher_succeeds():
    """An update must not fail because Claude still has flanner-mcp open."""
    desktop.link()
    held = subprocess.Popen(  # noqa: S603 - a launcher this test just wrote
        [str(_launcher("flanner-mcp"))],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_env(),
    )
    try:
        made = desktop.link()
        assert _launcher("flanner-mcp") in made
        set_aside = sorted(desktop.bin_dir().glob(f"*{desktop.SET_ASIDE}*"))
        assert set_aside, "the old launchers should have been moved aside, not overwritten"
    finally:
        assert held.stdin is not None
        held.stdin.close()
        held.wait(timeout=120)

    desktop.link()
    assert not any(old.exists() for old in set_aside), "set-aside launchers outlived their use"


def test_the_hidden_command_prints_what_it_linked():
    result = CliRunner().invoke(cli, ["desktop-link"])

    assert result.exit_code == 0, result.output
    assert str(_launcher("flanner-mcp")) in result.output.replace("\n", "")


# --- the setup screen ---------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("", r"C:\f\bin"),
        (r"C:\a;C:\b", r"C:\f\bin;C:\a;C:\b"),
        (r"C:\a;c:\F\BIN\;C:\b", r"C:\f\bin;C:\a;C:\b"),
        (r"C:\f\bin;C:\a", r"C:\f\bin;C:\a"),
        (r"C:\a;;C:\b;", r"C:\f\bin;C:\a;C:\b"),
    ],
)
def test_the_launchers_go_first_on_path_and_only_once(before, after):
    """First, so the app's flanner wins over a pip one somebody left behind."""
    if sys.platform != "win32" and r"c:\F" in before:
        pytest.skip("Windows compares paths without case")
    assert desktop.path_with_first(before, r"C:\f\bin") == after


def test_windows_path_is_written_once_and_announced(monkeypatch):
    registry = {"value": r"C:\Tools", "kind": 2}
    announced = []
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_read_user_path", lambda: (registry["value"], registry["kind"]))
    monkeypatch.setattr(
        desktop, "_write_user_path", lambda value, kind: registry.update(value=value, kind=kind)
    )
    monkeypatch.setattr(desktop, "_announce_environment_change", lambda: announced.append(1))

    first = desktop.add_to_path(Path(r"C:\f\bin"))
    second = desktop.add_to_path(Path(r"C:\f\bin"))

    assert registry == {"value": r"C:\f\bin;C:\Tools", "kind": 2}
    assert first and not second
    assert announced == [1]


def test_shell_profiles_get_one_line_each():
    home = Path.home()  # a temp dir, per conftest, not created until used
    home.mkdir(parents=True)
    (home / ".bashrc").write_text("alias ll='ls -l'", encoding="utf-8")

    folder = Path("/home/me/.flanner/bin")
    changed = desktop._add_to_profiles(folder)
    again = desktop._add_to_profiles(folder)

    assert home / ".profile" in changed and home / ".bashrc" in changed
    assert again == []
    bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert bashrc.startswith("alias ll='ls -l'\n")
    assert bashrc.count(desktop.PROFILE_MARK) == 1
    assert f'export PATH="{folder}:$PATH"' in bashrc


def _fake_flanner(folder: Path, version: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        (folder / "flanner.bat").write_text(f"@echo flanner, version {version}\n")
    else:
        script = folder / "flanner"
        script.write_text(f"#!/bin/sh\necho 'flanner, version {version}'\n")
        script.chmod(0o755)


def test_a_pip_flanner_on_path_is_found_with_its_version(tmp_path, monkeypatch):
    ours = tmp_path / "ours"
    _fake_flanner(ours, "0.15.0")
    _fake_flanner(tmp_path / "pip", "0.12.0")
    monkeypatch.setenv("PATH", os.pathsep.join([str(ours), str(tmp_path / "pip")]))

    found = desktop.other_flanner(ours)

    assert found is not None
    assert Path(found["path"]).parent == tmp_path / "pip"
    assert found["version"] == "0.12.0"


def test_only_the_apps_own_flanner_means_none(tmp_path, monkeypatch):
    _fake_flanner(tmp_path / "ours", "0.15.0")
    monkeypatch.setenv("PATH", str(tmp_path / "ours"))
    assert desktop.other_flanner(tmp_path / "ours") is None


def test_the_probe_says_where_connecting_would_write(monkeypatch):
    monkeypatch.setenv("PATH", "")
    result = CliRunner().invoke(cli, ["desktop-probe"])

    assert result.exit_code == 0, result.output
    shown = json.loads(result.output)
    assert shown["bin"] == str(desktop.bin_dir())
    assert shown["claude_md"].endswith("CLAUDE.md")
    assert shown["other_flanner"] is None
    assert set(shown) == {
        "bin",
        "claude_desktop",
        "claude_code",
        "claude_md",
        "codex",
        "other_flanner",
    }


def test_connecting_puts_flanner_on_path_before_registering(monkeypatch):
    """Claude Code and Codex start the bare flanner-mcp, so PATH has to be first."""
    import flanner.cli as cli_module

    order = []
    monkeypatch.setattr(desktop, "add_to_path", lambda: order.append("path") or ["Added it."])
    monkeypatch.setattr(cli_module, "_register_agents_globally", lambda: order.append("register"))

    result = CliRunner().invoke(cli, ["desktop-connect"])

    assert result.exit_code == 0, result.output
    assert order == ["path", "register"]
