"""The desktop app itself, on Windows (the desktop PRD, section 12, layer 3).

    cargo build --manifest-path desktop/src-tauri/Cargo.toml
    FLANNER_DESKTOP_APP=desktop/src-tauri/target/debug/flanner-desktop.exe \\
    FLANNER_RUNTIME=build/runtime pytest desktop/tests/e2e

WebView2 opens a DevTools port when asked through an environment variable,
so Playwright attaches to the app's real window over CDP and checks what
runs inside it. macOS and Linux webviews have no such port; they get
tauri-driver instead.

The app runs against a temp FLANNER_HOME and a temp app folder, so it never
touches this machine's store, launchers or settings. The setup screen is
answered with "Connect" unticked, so PATH and the agents' configs are left
alone too.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page

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


def _what_webview_did(port: int) -> str:
    """Why the port may not be there, from Windows: WebView2's processes and their
    command lines, the runtime's version, any Edge policy, who holds the port, and
    proxies. On a hosted runner this is the only view there is."""
    return _powershell(
        "$ErrorActionPreference = 'SilentlyContinue';"
        " '--- msedgewebview2 processes ---';"
        " Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" |"
        "   ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.CommandLine };"
        " '--- WebView2 runtime ---';"
        " foreach ($k in 'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',"
        "   'HKCU:\\SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}') {"
        "   $v = (Get-ItemProperty $k).pv; if ($v) { $k + ' ' + $v } };"
        " '--- Edge and WebView2 policies ---';"
        " foreach ($k in 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Edge', 'HKCU:\\SOFTWARE\\Policies\\Microsoft\\Edge',"
        "   'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Edge\\WebView2', 'HKCU:\\SOFTWARE\\Policies\\Microsoft\\Edge\\WebView2') {"
        "   if (Test-Path $k) { $k; Get-ItemProperty $k | Format-List | Out-String -Width 300 } };"
        f" '--- port {port} ---'; netstat -ano | Select-String ':{port} ' | ForEach-Object {{ $_.Line }};"
        " '--- proxies ---';"
        " Get-ChildItem Env: | Where-Object Name -match 'proxy' | ForEach-Object { $_.Name + '=' + $_.Value }"
    )


class App:
    def __init__(self, process: subprocess.Popen[bytes], devtools: int, data: Path) -> None:
        self.process = process
        self.devtools = devtools
        self.data = data


def _what_the_app_said(output: Path, data: Path) -> str:
    """The app's own console output and the logs it wrote, for a failure message."""
    said = [f"--- {output.name} ---\n{output.read_text(errors='replace')[-4000:]}"]
    for log in sorted((data / "logs").glob("*.log")):
        said.append(f"--- {log.name} ---\n{log.read_text(errors='replace')[-2000:]}")
    return "\n".join(said)


def _wait_for_devtools(
    process: subprocess.Popen[bytes], port: int, output: Path, data: Path
) -> None:
    """Block until WebView2's DevTools port answers, or fail saying why it did not.

    The port only exists once the app has built its window. An app that
    exited, or never got that far, used to surface as a bare ECONNREFUSED
    from Playwright a minute later, with nothing from the app itself.
    """
    deadline = time.monotonic() + 120
    while True:
        if process.poll() is not None:
            pytest.fail(
                f"the app exited with {process.returncode} before its DevTools port opened\n"
                + _what_the_app_said(output, data)
            )
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            pytest.fail(
                f"the app is running but WebView2's DevTools port {port} never opened\n"
                + _what_the_app_said(output, data)
                + "\n"
                + _what_webview_did(port)
            )
        time.sleep(0.5)


@pytest.fixture(scope="module")
def app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[App]:
    devtools = _free_port()
    data = tmp_path_factory.mktemp("app-data")
    env = {
        **os.environ,
        "FLANNER_HOME": str(tmp_path_factory.mktemp("flanner-home")),
        "FLANNER_NO_KEYCHAIN": "1",
        "FLANNER_DESKTOP_RUNTIME": str(Path(str(RUNTIME)).resolve()),
        "FLANNER_DESKTOP_DATA": str(data),
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={devtools}",
        "RUST_BACKTRACE": "1",
    }
    # To a file: left on the inherited console, a panic at start never
    # reached the job log.
    output = tmp_path_factory.mktemp("app-output") / "app-console.log"
    with output.open("wb") as console:
        process = subprocess.Popen(
            [str(Path(str(APP)).resolve())], env=env, stdout=console, stderr=subprocess.STDOUT
        )
    try:
        _wait_for_devtools(process, devtools, output, data)
        yield App(process, devtools, data)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


class Window:
    """The app's window over CDP.

    Moving from the app's own origin to flanner's is a cross-origin
    navigation, which WebView2 reports as a new target, and a connection
    made before it never hears of it. So `flanner()` connects again until
    the new target is there.
    """

    def __init__(self, playwright: Any, devtools: int) -> None:
        self.playwright = playwright
        self.url = f"http://127.0.0.1:{devtools}"
        self.browser: Browser = self._connect()

    def _connect(self) -> Browser:
        deadline = time.monotonic() + 60
        while True:
            try:
                return self.playwright.chromium.connect_over_cdp(self.url)
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    def pages(self) -> list[Page]:
        return [page for context in self.browser.contexts for page in context.pages]

    def first(self) -> Page:
        return self.pages()[0]

    def flanner(self) -> Page:
        deadline = time.monotonic() + 120
        while True:
            for page in self.pages():
                if page.url.startswith("http://127.0.0.1:"):
                    page.wait_for_load_state()
                    return page
            if time.monotonic() > deadline:
                urls = [page.url for page in self.pages()]
                pytest.fail(f"the window never left the loading page: {urls}")
            time.sleep(1)
            self.browser.close()
            self.browser = self._connect()


@pytest.fixture(scope="module")
def window(app: App) -> Iterator[Window]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        connected = Window(playwright, app.devtools)
        yield connected
        connected.browser.close()


def test_a_first_start_asks_with_connect_already_ticked(window: Window) -> None:
    page = window.first()
    page.get_by_role("heading", name="Set up flanner").wait_for(timeout=120_000)

    assert page.get_by_role("checkbox", name="Connect flanner to Claude and Codex").is_checked()
    changes = page.locator("#changes").inner_text()
    assert "PATH" in changes
    assert "CLAUDE.md" in changes


def test_the_answer_starts_flanner_and_is_remembered(app: App, window: Window) -> None:
    page = window.first()
    page.get_by_role("checkbox", name="Connect flanner to Claude and Codex").uncheck()
    page.get_by_role("button", name="Continue").click()

    flanner = window.flanner()

    assert flanner.title() == "Dashboard"
    saved: dict[str, Any] = json.loads((app.data / "settings.json").read_text(encoding="utf-8"))
    assert saved["flanner"] == "bundled"
    assert saved["connected"] is False
    assert saved["version"]  # remembered, so the next version can say it was updated


def test_the_page_may_open_the_folder_dialog(window: Window) -> None:
    """An argument error, not a permission error, means the capability let it through.

    Calling it for real would open a dialog nobody on a runner can close.
    """
    answer = window.flanner().evaluate(
        "window.__TAURI__.core.invoke('plugin:dialog|open', {options: 123})"
        ".then(() => 'opened', e => String(e))"
    )
    assert "invalid args" in answer


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("plugin:dialog|message", {"message": "x"}),
        ("finish_setup", {"connect": True, "keepInstalled": False}),
        ("pending_setup", {}),
    ],
)
def test_the_page_may_do_nothing_else(window: Window, command: str, args: dict[str, Any]) -> None:
    """flanner's page is served over http; only the app's own page may answer setup."""
    answer = window.flanner().evaluate(
        "([command, args]) => window.__TAURI__.core.invoke(command, args)"
        ".then(() => 'allowed', e => String(e))",
        [command, args],
    )
    assert "not allowed" in answer


def test_flanner_web_cannot_outlive_the_app(app: App, window: Window) -> None:
    """Killed outright, not quit: Windows' job object must take flanner web down too."""
    window.flanner()
    web = _python_children(app.process.pid)
    assert web, "the app is not running flanner web"
    app.process.kill()
    app.process.wait(timeout=30)
    deadline = time.monotonic() + 15
    while any(_alive(pid) for pid in web):
        assert time.monotonic() < deadline, f"flanner web outlived the app: {web}"
        time.sleep(0.5)
