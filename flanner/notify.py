"""A desktop notification, the way each operating system already offers one.

Standard library only: a subprocess to the tool the platform ships
(`osascript` on macOS, `notify-send` on Linux, PowerShell's toast API on
Windows). No new dependency for a nicety. Never raises: a notification
that cannot be shown is not an error anybody can act on.

Clicking opens the thread where the platform allows it: Windows always;
macOS when `terminal-notifier` is installed (osascript's notifications
open Script Editor); Linux when `notify-send` has `--action` (libnotify
0.7.9, 2020), which waits for the click, so call this off any path that
must not wait.

No shell anywhere. The text goes through environment variables where a
script reads it (Windows, osascript) and as separate arguments otherwise,
so a teammate's name cannot become a command.

`FLANNER_DESKTOP_NOTIFICATIONS=off` turns them off; the test suite does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ENV = "FLANNER_DESKTOP_NOTIFICATIONS"

# Homebrew's bin directories, which a LaunchAgent's PATH does not include.
_BREW = ("/opt/homebrew/bin", "/usr/local/bin")
# How long a clickable Linux notification is waited on before giving up.
# ponytail: a thread per unclicked notification for up to an hour; fine at
# messaging's rate limits.
_CLICK_WAIT = 3600

_WINDOWS = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType = WindowsRuntime] > $null;"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
    "ContentType = WindowsRuntime] > $null;"
    # Every value is escaped before it becomes XML, so nothing in a name or a
    # link can change the notification's markup.
    "$e = { param($s) [System.Security.SecurityElement]::Escape($s) };"
    "$launch = if ($env:FLANNER_NOTE_URL) "
    "{ \" activationType='protocol' launch='\" + "
    "(& $e $env:FLANNER_NOTE_URL) + \"'\" } else { '' };"
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
    "$x.LoadXml(\"<toast$launch><visual><binding template='ToastGeneric'><text>\" + "
    "(& $e $env:FLANNER_NOTE_TITLE) + '</text><text>' + (& $e $env:FLANNER_NOTE_TEXT) + "
    "'</text></binding></visual></toast>');"
    # PowerShell's own registered app id: Windows drops a toast from an id
    # it has never seen, and registering one needs an installer.
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    r"'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe')"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
)
_MAC = (
    'display notification (system attribute "FLANNER_NOTE_TEXT") '
    'with title (system attribute "FLANNER_NOTE_TITLE")'
)


def enabled() -> bool:
    return os.environ.get(ENV, "on").strip().lower() not in ("0", "off", "false", "no")


def desktop(title: str, text: str, url: str = "") -> bool:
    """Show one notification. True if the platform tool accepted it.

    `url` is opened when the notification is clicked, where the platform
    allows it (see the module docstring); elsewhere the text alone shows.
    """
    if not enabled():
        return False
    env = {
        **os.environ,
        "FLANNER_NOTE_TITLE": title,
        "FLANNER_NOTE_TEXT": text,
        "FLANNER_NOTE_URL": url,
    }
    # A plain str, so mypy checks every branch on every OS.
    platform: str = sys.platform
    if platform == "win32":
        return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS], env)
    if platform == "darwin":
        search = os.pathsep.join([os.environ.get("PATH", ""), *_BREW])
        notifier = shutil.which("terminal-notifier", path=search)
        if url and notifier:
            # A value starting with "-" would be read as an option name.
            command = [notifier, "-title", _plain(title), "-message", _plain(text)]
            if _run([*command, "-open", url], env):
                return True
        return _run(["osascript", "-e", _MAC], env)
    if not shutil.which("notify-send"):
        return False
    if url:
        clicked = _run_output(
            ["notify-send", "--action=default=Open", "--wait", "--", title, text], env
        )
        if clicked is not None:
            if clicked.strip() == "default":
                import webbrowser

                webbrowser.open(url)
            return True
        # An older notify-send has no --action: show it without the click.
    return _run(["notify-send", "--", title, text], env)


def _plain(value: str) -> str:
    return " " + value if value.startswith("-") else value


def _run(command: list[str], env: dict[str, str]) -> bool:
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, env=env, capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _run_output(command: list[str], env: dict[str, str]) -> str | None:
    """Stdout of a command that waits for the person, or None if it failed."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, env=env, capture_output=True, text=True, timeout=_CLICK_WAIT, check=False
        )
    except subprocess.TimeoutExpired:
        return ""
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None
