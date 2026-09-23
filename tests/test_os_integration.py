"""The receiver at login and desktop notifications, on the real operating system.

Every other test of `autostart` and `notify` fakes the calls they make, so
nothing had run `systemctl --user`, `launchctl` or `notify-send` before a
user's machine did. These make those calls for real, which changes the
machine they run on: a service is registered and started, and a
notification is shown. So they run only on a throwaway CI runner or
container, and only when asked:

    FLANNER_OS_INTEGRATION=1 pytest -m os_integration

Linux needs a user systemd manager (`loginctl enable-linger`), and for the
notifications `notify-send`, `gdbus` and python3-dbusmock. macOS needs a
logged-in session; terminal-notifier is used when it is installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from flanner import autostart, notify

pytestmark = [
    pytest.mark.os_integration,
    pytest.mark.skipif(
        os.environ.get("FLANNER_OS_INTEGRATION") != "1",
        reason="changes this machine's services; set FLANNER_OS_INTEGRATION=1 on a throwaway one",
    ),
    pytest.mark.skipif(sys.platform == "win32", reason="Windows was walked through by hand"),
]

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Text a teammate controls. None of it may run, and all of it must arrive as written.
HOSTILE_TITLE = '-rf Ben "Otieno" (@ben)'


def hostile_text(where: Path) -> str:
    return f"drop it? $(touch {where}/ran) `touch {where}/ran2`; echo '{where}' > {where}/ran3"


@pytest.fixture(autouse=True)
def _the_real_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo two of the suite's isolations: these tests exist to cross them.

    The service manager reads the real home directory, not the fake one the
    suite points HOME at, and a notification test with notifications off
    proves nothing.
    """
    import pwd

    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    monkeypatch.setenv(notify.ENV, "on")


def flanner(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "flanner", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# --- the receiver at login ------------------------------------------------------


@pytest.fixture
def signed_in_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A flanner home with a signed-in session, so `peer serve` keeps running."""
    home = tmp_path / "flanner-home"
    monkeypatch.setenv("FLANNER_HOME", str(home))
    seeded = flanner("demo", "seed", "--home", str(home), "--signed-in")
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    yield home
    if autostart.enabled():
        autostart.disable()


def _label() -> str:
    return f"io.flanner.{autostart.name()}"


def _registered() -> bool:
    """Whether the service manager itself, not only a file on disk, knows the receiver."""
    if sys.platform == "darwin":
        return _run("launchctl", "list", _label()).returncode == 0
    shown = _run("systemctl", "--user", "is-enabled", f"{autostart.name()}.service")
    return shown.stdout.strip() == "enabled"


def _diagnose(home: Path) -> str:
    """What the service manager and the receiver said, for a failure message."""
    if sys.platform == "darwin":
        listed = _run("launchctl", "list", _label())
        log = home / "receiver.log"
        return listed.stdout + listed.stderr + (log.read_text() if log.exists() else "(no log)")
    unit = f"{autostart.name()}.service"
    status = _run("systemctl", "--user", "status", "--no-pager", unit)
    journal = _run("journalctl", "--user", "-u", unit, "--no-pager", "-n", "40")
    return status.stdout + status.stderr + journal.stdout + journal.stderr


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _heartbeat(home: Path, timeout: float) -> dict[str, float]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            beat: dict[str, float] = json.loads((home / autostart.HEARTBEAT).read_text())
            return beat
        except (OSError, ValueError):
            time.sleep(1)
    raise AssertionError(f"no heartbeat within {timeout:.0f}s:\n{_diagnose(home)}")


def test_autostart_starts_a_receiver_now_and_off_stops_it(signed_in_home: Path) -> None:
    """`on` registers the receiver and starts it; it beats and stays up; `off` ends it.

    A folder named `flanner` in the home directory is where a checkout often
    sits, and it is what `python -m flanner` imports instead of the package
    when a process starts from there.
    """
    decoy = Path.home() / "flanner"
    made_decoy = not decoy.exists()
    decoy.mkdir(exist_ok=True)
    try:
        on = flanner("peer", "autostart", "on")
        assert on.returncode == 0, on.stdout + on.stderr
        assert autostart.enabled()
        assert _registered(), _diagnose(signed_in_home)

        first = _heartbeat(signed_in_home, timeout=60)
        pid = int(first["pid"])
        # Past its first beat, which comes before the network is up: a
        # receiver that beats and then dies must not pass.
        time.sleep(15)
        assert _alive(pid), f"the receiver exited:\n{_diagnose(signed_in_home)}"

        off = flanner("peer", "autostart", "off")
        assert off.returncode == 0, off.stdout + off.stderr
        assert not autostart.enabled()
        assert not _registered(), _diagnose(signed_in_home)
        deadline = time.monotonic() + 20
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(1)
        assert not _alive(pid), "autostart off left the receiver running"
    finally:
        if made_decoy:
            decoy.rmdir()


# --- desktop notifications ------------------------------------------------------


@pytest.fixture
def notification_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private session bus with python-dbusmock answering as the notification server.

    The system Python runs the mock, since that is where the distribution
    installs python3-dbusmock. Its log records every call it answered.
    """
    bus = subprocess.Popen(  # noqa: S603
        ["dbus-daemon", "--session", "--print-address=1", "--nofork"],  # noqa: S607
        stdout=subprocess.PIPE,
        text=True,
    )
    assert bus.stdout is not None
    address = bus.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    log = tmp_path / "notifications.log"
    capabilities = json.dumps({"capabilities": "body actions"})
    mock = subprocess.Popen(  # noqa: S603
        ["/usr/bin/python3", "-m", "dbusmock", "--session", "-t", "notification_daemon"]
        + ["-p", capabilities, "-l", str(log)],
    )
    try:
        deadline = time.monotonic() + 15
        while _gdbus_call("org.freedesktop.Notifications.GetServerInformation").returncode:
            assert time.monotonic() < deadline, "the mock notification server never answered"
            time.sleep(0.3)
        yield log
    finally:
        mock.terminate()
        bus.terminate()
        mock.wait(timeout=10)
        bus.wait(timeout=10)


def _gdbus_call(method: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(
        "gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
        "--object-path", "/org/freedesktop/Notifications", "--method", method, *args,
    )  # fmt: skip


def _notified(log: Path, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = log.read_text().splitlines() if log.exists() else []
        calls = [line for line in lines if " Notify " in line]
        if calls:
            return calls[-1]
        time.sleep(0.2)
    raise AssertionError("no Notify call reached the notification server")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="notify-send is the Linux path")
def test_a_linux_notification_arrives_as_written_and_its_click_opens_the_thread(
    notification_server: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = tmp_path / "opened"
    browser = tmp_path / "browser"
    browser.write_text(f'#!/bin/sh\nprintf %s "$1" > "{opened}"\n')
    browser.chmod(0o755)
    monkeypatch.setenv("BROWSER", f"{browser} %s")
    url = "http://127.0.0.1:8080/mesh/messages/abc123"
    text = hostile_text(tmp_path)

    # Off the calling thread, as the daemon does: a clickable notification
    # waits for the person.
    shown: list[bool] = []

    def show() -> None:
        shown.append(notify.desktop(HOSTILE_TITLE, text, url))

    worker = threading.Thread(target=show)
    worker.start()

    call = _notified(notification_server)
    assert f'"{HOSTILE_TITLE}"' in call and f'"{text}"' in call, call
    assert '"default", "Open"' in call, call

    # The person clicks it: what a notification server sends then.
    _gdbus_call(
        "org.freedesktop.DBus.Mock.EmitSignal",
        "org.freedesktop.Notifications", "ActionInvoked", "us", "[<uint32 1>, <'default'>]",
    )  # fmt: skip
    _gdbus_call(
        "org.freedesktop.DBus.Mock.EmitSignal",
        "org.freedesktop.Notifications", "NotificationClosed", "uu", "[<uint32 1>, <uint32 2>]",
    )  # fmt: skip
    worker.join(timeout=15)

    assert shown == [True]
    assert opened.read_text() == url
    assert not list(tmp_path.glob("ran*")), "text from a teammate ran as a command"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="notify-send is the Linux path")
def test_a_linux_notification_without_a_link_is_shown_plainly(notification_server: Path) -> None:
    assert notify.desktop("Chen Wu (@chen)", "the deploy is done") is True
    call = _notified(notification_server)
    assert '"Chen Wu (@chen)"' in call and '"the deploy is done"' in call, call
    assert '"default"' not in call, call


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript is the macOS path")
def test_a_macos_notification_is_shown_and_runs_nothing_it_carries(tmp_path: Path) -> None:
    assert notify.desktop(HOSTILE_TITLE, hostile_text(tmp_path)) is True
    assert not list(tmp_path.glob("ran*")), "text from a teammate ran as a command"


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("terminal-notifier"),
    reason="the clickable macOS path needs terminal-notifier",
)
def test_a_macos_notification_with_a_link_goes_through_terminal_notifier(tmp_path: Path) -> None:
    url = "http://127.0.0.1:8080/mesh/messages/abc123"
    assert notify.desktop(HOSTILE_TITLE, hostile_text(tmp_path), url) is True
    assert not list(tmp_path.glob("ran*")), "text from a teammate ran as a command"
