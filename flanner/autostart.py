"""Start receiving at login, so a teammate's message always has somewhere to land.

The mesh messaging plan, section 10.5. A message only arrives while this
device runs `flanner peer serve`, and a desktop notification is the one way
to reach somebody who never types anything. So receiving has to survive a
reboot, and each platform's own per-user mechanism does that without admin
rights:

- Windows: a value under `HKCU\\...\\CurrentVersion\\Run`, which runs
  `flanner peer start` at login with no console window. The same command
  also runs once when it is registered, as launchctl and systemctl do.
- macOS: a LaunchAgent in `~/Library/LaunchAgents`.
- Linux: a systemd user service in `~/.config/systemd/user`.

One registration per flanner home, named from the home's path, so the two
test homes of the plan's section 21 do not collide with a real one. `off`
removes exactly what `on` registered and nothing else.

Receiving is also a heartbeat: the serving process touches `receiver.json`
every 30 seconds, and `receiving()` reads it. That answers "is anything
accepting messages here?" the same way on every platform.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import identity

HEARTBEAT = "receiver.json"
HEARTBEAT_FRESH_SECONDS = 90
DECLINED = "autostart-declined"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_MEANWHILE = "flanner peer start receives until you restart."


class AutostartError(Exception):
    """The login service was not set up. The message says what to do instead."""


def _platform() -> str:
    """Read at call time, and through a function so every branch is checked.

    Tests switch it, and a type checker running on one platform would
    otherwise call the other platforms' branches unreachable.
    """
    return sys.platform


# The Windows Run key. Each helper checks `sys.platform` itself, written
# out, because that is the one form a type checker reads as a platform
# check: on Linux and macOS it skips the block instead of reporting that
# `winreg` has none of these names. `_platform()` still chooses the branch,
# so tests can switch it.


def _registry_set(command: str) -> None:
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name(), 0, winreg.REG_SZ, command)


def _registry_delete() -> bool:
    removed = False
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, name())
            removed = True
        except FileNotFoundError:
            pass
    return removed


def _registry_has() -> bool:
    found = False
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, name())
            found = True
        except FileNotFoundError:
            pass
    return found


def _launch_now(command: str) -> None:
    """Start the receiver now, with the command the Run entry uses at login."""
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # Our own interpreter, and a command this module wrote.
        subprocess.Popen(command, creationflags=flags, close_fds=True)  # noqa: S603


def _home() -> Path:
    return identity.flanner_home()


def name() -> str:
    """This home's registration name, stable for the home's path."""
    digest = hashlib.sha256(str(_home().resolve()).encode("utf-8")).hexdigest()[:8]
    return f"flanner-receiver-{digest}"


def _log() -> Path:
    return _home() / "receiver.log"


def _pythonw() -> str:
    """The windowless interpreter beside this one, so login shows no console."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else exe)


def _windows_command() -> str:
    # `peer start` rather than `serve`: it records the pid, so `peer stop`
    # and `peer start` see this receiver, and it refuses to start a second.
    # Output goes to a file because a windowless interpreter has no console.
    # `-c` puts the starting directory first on the import path, and at
    # login that is the user's home, where a folder named `flanner` (a
    # checkout, say) would be imported instead of the package. So the
    # empty entry goes, and the process starts from the flanner home.
    script = (
        "import os,sys;sys.path[:]=[p for p in sys.path if p];"
        f"os.chdir({str(_home())!r});"
        f"os.environ['FLANNER_HOME']={str(_home())!r};"
        f"log=open({str(_log())!r},'a',encoding='utf-8');sys.stdout=sys.stderr=log;"
        "from flanner.cli import main;sys.argv=['flanner','peer','start'];main()"
    )
    return f'"{_pythonw()}" -c "{script}"'


#: How macOS and Linux start the receiver. Not `-m flanner`: systemd starts a
#: user service in the home directory, and `-m` puts that first on the
#: import path, where a folder named `flanner` (a checkout) was imported
#: instead of the package and the receiver never started. The empty entry
#: goes before flanner is imported, as in the agent hooks.
_MAIN = "import sys;sys.path[:]=[p for p in sys.path if p];from flanner.cli import main;main()"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"io.flanner.{name()}.plist"


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{name()}.service"


def _run(*argv: str) -> bool:
    try:
        done = subprocess.run(argv, capture_output=True, timeout=30, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def enable() -> str:
    """Register the receiver to start at login. Returns where it was registered."""
    (_home() / DECLINED).unlink(missing_ok=True)
    if _platform() == "win32":
        command = _windows_command()
        _registry_set(command)
        # launchctl and systemctl start the receiver as they register it; the
        # Run entry only runs at the next login, so start it now as well.
        if not receiving():
            _launch_now(command)
        return rf"HKCU\{_RUN_KEY}\{name()}"
    if _platform() == "darwin":
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            plistlib.dumps(
                {
                    "Label": f"io.flanner.{name()}",
                    "ProgramArguments": [sys.executable, "-c", _MAIN, "peer", "serve"],
                    "EnvironmentVariables": {"FLANNER_HOME": str(_home())},
                    "RunAtLoad": True,
                    "KeepAlive": {"SuccessfulExit": False},
                    "StandardOutPath": str(_log()),
                    "StandardErrorPath": str(_log()),
                }
            )
        )
        if not _run("launchctl", "load", "-w", str(path)):
            disable()
            raise AutostartError(
                "launchctl could not load the login agent, so nothing starts at login. "
                f"{_MEANWHILE}"
            )
        return str(path)
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Unit]\nDescription=flanner: receive messages from teammates\n\n"
        "[Service]\n"
        f'Environment="FLANNER_HOME={_home()}"\n'
        f'ExecStart="{sys.executable}" -c "{_MAIN}" peer serve\n'
        "Restart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    if not (
        _run("systemctl", "--user", "daemon-reload")
        and _run("systemctl", "--user", "enable", "--now", f"{name()}.service")
    ):
        disable()
        raise AutostartError(
            "systemctl --user could not start the service; it needs a systemd user "
            f"session. {_MEANWHILE}"
        )
    return str(path)


def disable() -> bool:
    """Remove this home's registration. True if there was one."""
    if _platform() == "win32":
        return _registry_delete()
    if _platform() == "darwin":
        path = _plist_path()
        if not path.exists():
            return False
        _run("launchctl", "unload", "-w", str(path))
        path.unlink()
        return True
    path = _unit_path()
    if not path.exists():
        return False
    _run("systemctl", "--user", "disable", "--now", f"{name()}.service")
    path.unlink()
    _run("systemctl", "--user", "daemon-reload")
    return True


def enabled() -> bool:
    if _platform() == "win32":
        return _registry_has()
    return (_plist_path() if _platform() == "darwin" else _unit_path()).exists()


def decline() -> None:
    """Remember a no, so nobody is asked again."""
    _home().mkdir(parents=True, exist_ok=True)
    (_home() / DECLINED).write_text("declined\n", encoding="utf-8")


def declined() -> bool:
    return (_home() / DECLINED).exists()


# --- is anything receiving? ----------------------------------------------------


def beat() -> None:
    """Called by the serving process: this device is accepting messages now."""
    path = _home() / HEARTBEAT
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8")
    os.replace(temp, path)


def receiving(*, now: float | None = None) -> bool:
    """Whether a receiver has beaten within the last 90 seconds."""
    try:
        data: dict[str, Any] = json.loads((_home() / HEARTBEAT).read_text(encoding="utf-8"))
        return (now or time.time()) - float(data["at"]) < HEARTBEAT_FRESH_SECONDS
    except (OSError, ValueError, KeyError, TypeError):
        return False
