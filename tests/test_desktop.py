"""The launchers the desktop app points at the flanner it bundles.

FLANNER_HOME is a temp dir for every test (conftest), so these write
launchers there and never into the developer's own ~/.flanner/bin.
"""

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
