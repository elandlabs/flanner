"""The desktop app itself, on Windows (the desktop PRD, section 12, layer 3).

    cargo build --manifest-path desktop/src-tauri/Cargo.toml
    FLANNER_DESKTOP_APP=desktop/src-tauri/target/debug/flanner-desktop.exe \\
    FLANNER_RUNTIME=build/runtime pytest desktop/tests/e2e

WebView2 opens a DevTools port when asked through an environment variable,
so Playwright attaches to the app's real window over CDP and checks what
runs inside it. macOS and Linux webviews have no such port; they get
tauri-driver instead.

The app runs against a temp FLANNER_HOME, so it never touches this
machine's store or its launchers.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

APP = os.environ.get("FLANNER_DESKTOP_APP")
RUNTIME = os.environ.get("FLANNER_RUNTIME")
pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="WebView2's DevTools port is Windows-only"),
    pytest.mark.skipif(
        not (APP and RUNTIME), reason="set FLANNER_DESKTOP_APP and FLANNER_RUNTIME"
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _powershell(script: str) -> str:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def _python_children(pid: int) -> list[int]:
    """The python processes the app started, from Windows' own process list."""
    listed = _powershell(
        f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' |"
        " Where-Object Name -eq 'python.exe').ProcessId"
    )
    return [int(line) for line in listed.split()]


def _alive(pid: int) -> bool:
    return bool(_powershell(f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Id").strip())


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
    devtools = _free_port()
    env = {
        **os.environ,
        "FLANNER_HOME": str(tmp_path_factory.mktemp("flanner-home")),
        "FLANNER_NO_KEYCHAIN": "1",
        "FLANNER_DESKTOP_RUNTIME": str(Path(str(RUNTIME)).resolve()),
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={devtools}",
    }
    process = subprocess.Popen([str(Path(str(APP)).resolve())], env=env)
    try:
        yield process, devtools
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


@pytest.fixture(scope="module")
def page(app: tuple[subprocess.Popen[bytes], int]) -> Iterator[Page]:
    from playwright.sync_api import sync_playwright

    _, devtools = app
    with sync_playwright() as playwright:
        deadline = time.monotonic() + 120
        while True:
            try:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{devtools}")
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
                continue
            pages = [each for context in browser.contexts for each in context.pages]
            served = [each for each in pages if each.url.startswith("http://127.0.0.1:")]
            if served:
                yield served[0]
                browser.close()
                return
            browser.close()
            if time.monotonic() > deadline:
                pytest.fail(f"the window never left the loading page: {[p.url for p in pages]}")
            time.sleep(1)


def test_the_window_shows_flanners_own_web_ui(page: Page) -> None:
    page.wait_for_load_state()
    assert page.title() == "Dashboard"


def test_the_page_may_open_the_folder_dialog(page: Page) -> None:
    """An argument error, not a permission error, means the capability let it through.

    Calling it for real would open a dialog nobody on a runner can close.
    """
    answer = page.evaluate(
        "window.__TAURI__.core.invoke('plugin:dialog|open', {options: 123})"
        ".then(() => 'opened', e => String(e))"
    )
    assert "invalid args" in answer


def test_the_page_may_do_nothing_else(page: Page) -> None:
    answer = page.evaluate(
        "window.__TAURI__.core.invoke('plugin:dialog|message', {message: 'x'})"
        ".then(() => 'shown', e => String(e))"
    )
    assert "not allowed" in answer


def test_flanner_web_cannot_outlive_the_app(
    app: tuple[subprocess.Popen[bytes], int], page: Page
) -> None:
    """Killed outright, not quit: Windows' job object must take flanner web down too."""
    process, _ = app
    web = _python_children(process.pid)
    assert web, "the app is not running flanner web"
    process.kill()
    process.wait(timeout=30)
    deadline = time.monotonic() + 15
    while any(_alive(pid) for pid in web):
        assert time.monotonic() < deadline, f"flanner web outlived the app: {web}"
        time.sleep(0.5)
