"""What the desktop app asks of the flanner it bundles.

The app keeps each version's Python in its own folder and runs
`flanner desktop-link` from the new one after every install or update.
That points the fixed launchers in FLANNER_HOME/bin at the running Python,
so a config naming a launcher keeps working across updates.

Launchers are written on the machine that runs them, never shipped. The
ones pip writes hold the absolute path of the Python that wrote them, so a
launcher built in CI would name a CI runner's folder.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

#: The same two console scripts pyproject.toml declares.
LAUNCHERS = ("flanner = flanner.cli:main", "flanner-mcp = flanner.server:main")

#: A launcher moved aside because something was still running it.
SET_ASIDE = ".old-"


def bin_dir() -> Path:
    return Path(os.environ.get("FLANNER_HOME", Path.home() / ".flanner")) / "bin"


def link(target: Path | None = None) -> list[Path]:
    """Write launchers for this Python into `target`, and return their paths.

    Windows will not overwrite a launcher an agent is running, but it will
    rename one, so the old launcher is moved aside first. Set-aside files
    are removed by a later link, once whatever held them has exited.
    """
    # ponytail: pip's vendored distlib, the code pip itself writes launchers
    # with. A private import, but the app pins the pip it bundles, so it
    # cannot move under us; vendor distlib if the app ever stops shipping pip.
    import importlib

    distlib: Any = importlib.import_module("pip._vendor.distlib.scripts")  # untyped

    target = target or bin_dir()
    target.mkdir(parents=True, exist_ok=True)
    _remove_set_aside(target)
    stamp = time.time_ns()
    for spec in LAUNCHERS:
        name = spec.split(" = ")[0]
        for existing in (target / name, target / f"{name}.exe"):
            if existing.exists():
                existing.replace(existing.with_name(f"{existing.name}{SET_ASIDE}{stamp}"))

    maker = distlib.ScriptMaker(None, str(target), add_launchers=True)
    maker.executable = sys.executable
    maker.variants = {""}
    maker.clobber = True
    return [Path(path) for path in maker.make_multiple(list(LAUNCHERS))]


def _remove_set_aside(target: Path) -> None:
    for old in target.glob(f"*{SET_ASIDE}*"):
        try:
            old.unlink()
        except OSError:
            continue  # still running; the next link tries again
