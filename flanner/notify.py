"""A desktop notification, the way each operating system already offers one.

Standard library only: a subprocess to the tool the platform ships
(`osascript` on macOS, `notify-send` on Linux, PowerShell's toast API on
Windows). No new dependency for a nicety. Never raises and never blocks
for long: a notification that cannot be shown is not an error anybody can
act on.

The text goes to the child process through environment variables, never
spliced into a command line, so a teammate's name cannot become a command.

`FLANNER_DESKTOP_NOTIFICATIONS=off` turns them off; the test suite does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ENV = "FLANNER_DESKTOP_NOTIFICATIONS"

_WINDOWS = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType = WindowsRuntime] > $null;"
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$x = $t.GetElementsByTagName('text');"
    "$x.Item(0).AppendChild($t.CreateTextNode($env:FLANNER_NOTE_TITLE)) > $null;"
    "$x.Item(1).AppendChild($t.CreateTextNode($env:FLANNER_NOTE_TEXT)) > $null;"
    # PowerShell's own registered app id: Windows drops a toast from an id
    # it has never seen, and registering one needs an installer.
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    r"'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe')"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
)
_MAC = (
    'display notification (system attribute "FLANNER_NOTE_TEXT") '
    'with title (system attribute "FLANNER_NOTE_TITLE")'
)


def enabled() -> bool:
    return os.environ.get(ENV, "on").strip().lower() not in ("0", "off", "false", "no")


def desktop(title: str, text: str) -> bool:
    """Show one notification. True if the platform tool accepted it."""
    if not enabled():
        return False
    if sys.platform == "win32":
        command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS]
    elif sys.platform == "darwin":
        command = ["osascript", "-e", _MAC]
    elif shutil.which("notify-send"):
        command = ["notify-send", "--", title, text]
    else:
        return False
    env = {**os.environ, "FLANNER_NOTE_TITLE": title, "FLANNER_NOTE_TEXT": text}
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv; the text travels in env
            command, env=env, capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
