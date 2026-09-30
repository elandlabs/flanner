"""What the desktop app asks of the flanner it bundles.

The app keeps each version's Python in its own folder and runs
`flanner desktop-link` from the new one after every install or update.
That points the fixed launchers in FLANNER_HOME/bin at the running Python,
so a config naming a launcher keeps working across updates.

On a first start the app also asks `desktop-probe` what to show on its
setup screen, and runs `desktop-connect` when somebody leaves "Connect to
Claude and Codex" ticked.

Launchers are written on the machine that runs them, never shipped. The
ones pip writes hold the absolute path of the Python that wrote them, so a
launcher built in CI would name a CI runner's folder.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

#: The same two console scripts pyproject.toml declares.
LAUNCHERS = ("flanner = flanner.cli:main", "flanner-mcp = flanner.server:main")

#: A launcher moved aside because something was still running it.
SET_ASIDE = ".old-"

#: Ends the line `add_to_path` writes into a shell profile, so a second run
#: finds it instead of adding another.
PROFILE_MARK = "# added by the flanner desktop app"


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


def other_flanner(ours: Path | None = None) -> dict[str, str] | None:
    """The first flanner on PATH that is not the app's own, and its version.

    A pip install the app would otherwise shadow, so the setup screen can
    ask which one to keep using.
    """
    ours = (ours or bin_dir()).resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).resolve() == ours:
                continue
        except OSError:
            continue
        found = shutil.which("flanner", path=entry)
        if found:
            return {"path": found, "version": _version_of(found)}
    return None


def _version_of(program: str) -> str:
    try:
        shown = subprocess.run(  # noqa: S603 - a flanner found on PATH, fixed argv
            [program, "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    match = re.search(r"version (\S+)", shown)
    return match.group(1) if match else "unknown"


def probe() -> dict[str, Any]:
    """What the app's setup screen shows: exactly what connecting would change."""
    from .claude_integration import codex_config_path, codex_installed, get_claude_config_path

    desktop_config = get_claude_config_path()
    return {
        "bin": str(bin_dir()),
        "claude_desktop": str(desktop_config) if desktop_config else None,
        "claude_code": shutil.which("claude") is not None,
        "claude_md": str(Path.home() / ".claude" / "CLAUDE.md"),
        "codex": str(codex_config_path()) if codex_installed() else None,
        "other_flanner": other_flanner(),
    }


def add_to_path(folder: Path | None = None) -> list[str]:
    """Put `folder` first on the user's PATH for new terminals. Returns what changed.

    First, so the app's flanner wins over a pip one the person chose to
    leave behind. Windows keeps the user's PATH in the registry; macOS and
    Linux read it from shell profiles.
    """
    folder = folder or bin_dir()
    if sys.platform == "win32":
        value, kind = _read_user_path()
        updated = path_with_first(value, str(folder))
        if updated == value:
            return []
        _write_user_path(updated, kind)
        _announce_environment_change()
        return [f"Added {folder} to your PATH."]
    else:
        changed = _add_to_profiles(folder)
        return [f"Added {folder} to your PATH in {profile}." for profile in changed]


def path_with_first(value: str, folder: str) -> str:
    """`value`, a ;-separated Windows PATH, with `folder` first and only once."""

    def same(entry: str) -> bool:
        return os.path.normcase(os.path.expandvars(entry).rstrip("\\/")) == os.path.normcase(
            folder.rstrip("\\/")
        )

    entries = [entry for entry in value.split(";") if entry]
    if entries and same(entries[0]):
        return value
    return ";".join([folder, *(entry for entry in entries if not same(entry))])


def _read_user_path() -> tuple[str, int]:
    import winreg  # type: ignore[import-not-found,unused-ignore]  # Windows only

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ
    return str(value), int(kind)


def _write_user_path(value: str, kind: int) -> None:
    import winreg  # type: ignore[import-not-found,unused-ignore]  # Windows only

    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        kind = winreg.REG_EXPAND_SZ
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, kind, value)


def _announce_environment_change() -> None:
    """Tell Explorer, and so every terminal opened from now on, that PATH changed."""
    import ctypes

    result = ctypes.c_ulong()
    windll: Any = getattr(ctypes, "windll")  # noqa: B009 - absent off Windows, for mypy
    windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
    )


def _add_to_profiles(folder: Path) -> list[Path]:
    """Add one export line to each shell profile that is read, once."""
    home = Path.home()
    profiles = [home / ".profile"]
    profiles += [p for p in (home / ".bashrc", home / ".bash_profile") if p.is_file()]
    if sys.platform == "darwin" or (home / ".zshenv").is_file() or (home / ".zshrc").is_file():
        profiles.append(home / ".zshenv")
    line = f'export PATH="{folder}:$PATH"  {PROFILE_MARK}\n'
    changed = []
    for profile in profiles:
        text = profile.read_text(encoding="utf-8") if profile.is_file() else ""
        if PROFILE_MARK in text:
            continue
        separator = "" if not text or text.endswith("\n") else "\n"
        profile.write_text(text + separator + line, encoding="utf-8")
        changed.append(profile)
    return changed
