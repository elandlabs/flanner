"""Updates, on Windows (the desktop PRD, section 12, layer 4).

Needs a test build of the app, compiled with the throwaway key and a local
update address from update_signing.py:

    cd desktop/src-tauri
    TAURI_CONFIG="$(python ../tests/e2e/update_signing.py)" cargo build
    cd ../..
    FLANNER_DESKTOP_UPDATE_APP=desktop/src-tauri/target/debug/flanner-desktop.exe \\
    FLANNER_RUNTIME=build/runtime pytest desktop/tests/e2e/test_updates_windows.py

The app is told to install as soon as it finds an update (a test-build-only
switch), so no tray click is needed. The "installer" is a copy of Windows'
own hostname.exe: when the signature verifies, the updater runs it, it
prints one line and exits, and so does the app.

Tauri's own updater does the verifying here; these tests prove the
signature check is switched on and refuses a tampered file. They do not
test installing a real release: that needs two published installers, and
is on the release checklist.
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
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import update_signing  # noqa: E402

APP = os.environ.get("FLANNER_DESKTOP_UPDATE_APP")
RUNTIME = os.environ.get("FLANNER_RUNTIME")
pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="the installer path tested is Windows'"),
    pytest.mark.skipif(
        not (APP and RUNTIME), reason="set FLANNER_DESKTOP_UPDATE_APP and FLANNER_RUNTIME"
    ),
]

INSTALLER = "Flanner_99.0.0_x64-setup.exe"


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base's name
        pass


@pytest.fixture
def release(tmp_path: Path) -> Iterator[Path]:
    """A local update server announcing 99.0.0, on the port the test build asks."""
    served = tmp_path / "release"
    served.mkdir()
    handler = partial(Quiet, directory=str(served))
    server = ThreadingHTTPServer(("127.0.0.1", update_signing.PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield served
    finally:
        server.shutdown()
        server.server_close()


def _publish(served: Path, installer: bytes, signed_for: bytes) -> None:
    (served / INSTALLER).write_bytes(installer)
    entry = {
        "signature": update_signing.sign(signed_for, INSTALLER),
        "url": f"http://127.0.0.1:{update_signing.PORT}/{INSTALLER}",
    }
    latest = {
        "version": "99.0.0",
        "notes": "A test release.",
        "pub_date": "2026-09-23T00:00:00Z",
        "platforms": {"windows-x86_64": entry, "windows-x86_64-nsis": entry},
    }
    (served / "latest.json").write_text(json.dumps(latest), encoding="utf-8")


def _start(tmp_path: Path) -> tuple[subprocess.Popen[bytes], Path]:
    data = tmp_path / "app-data"
    data.mkdir()
    # Already set up, so the setup screen does not wait for an answer.
    (data / "settings.json").write_text(
        json.dumps({"flanner": "bundled", "connected": False, "version": "0.14.0"}),
        encoding="utf-8",
    )
    home = tmp_path / "flanner-home"
    env = {
        **os.environ,
        "FLANNER_HOME": str(home),
        "FLANNER_NO_KEYCHAIN": "1",
        "FLANNER_DESKTOP_RUNTIME": str(Path(str(RUNTIME)).resolve()),
        "FLANNER_DESKTOP_DATA": str(data),
        "FLANNER_DESKTOP_UPDATE_NOW": "1",
    }
    return subprocess.Popen(
        [str(Path(str(APP)).resolve())], env=env
    ), data / "logs" / "updates.log"


def _log_says(log: Path, words: str, seconds: float = 120) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.is_file() else ""
        if words in text:
            return text
        time.sleep(0.5)
    pytest.fail(f"updates.log never said {words!r}: {log.read_text() if log.is_file() else ''}")


def _hostname_exe() -> bytes:
    return (Path(os.environ["SYSTEMROOT"]) / "System32" / "hostname.exe").read_bytes()


def test_a_tampered_download_is_refused_and_the_app_keeps_running(
    tmp_path: Path, release: Path
) -> None:
    genuine = _hostname_exe()
    _publish(release, installer=genuine + b"\0", signed_for=genuine)
    app, log = _start(tmp_path)
    try:
        text = _log_says(log, "refused")
        assert "found 99.0.0" in text
        time.sleep(2)
        assert app.poll() is None, "the app exited after refusing an update"
    finally:
        app.kill()
        app.wait(timeout=30)


def test_a_signed_download_is_installed(tmp_path: Path, release: Path) -> None:
    genuine = _hostname_exe()
    _publish(release, installer=genuine, signed_for=genuine)
    app, log = _start(tmp_path)
    try:
        _log_says(log, "installing 99.0.0")
        # On Windows the updater starts the installer and exits the app.
        app.wait(timeout=60)
        assert "refused" not in log.read_text(encoding="utf-8")
    finally:
        if app.poll() is None:
            app.kill()
            app.wait(timeout=30)
        shutil.rmtree(tmp_path / "flanner-home", ignore_errors=True)
