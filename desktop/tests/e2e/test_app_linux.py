"""The desktop app itself, on Linux, through tauri-driver (desktop PRD section 12, layer 3).

    cargo install tauri-driver --locked       # and webkit2gtk-driver from apt
    cargo build --manifest-path desktop/src-tauri/Cargo.toml
    export FLANNER_DESKTOP_APP=$PWD/desktop/src-tauri/target/debug/flanner-desktop
    export FLANNER_DESKTOP_RUNTIME=$PWD/build/runtime FLANNER_NO_KEYCHAIN=1
    export FLANNER_HOME=$(mktemp -d) FLANNER_DESKTOP_DATA=$(mktemp -d)
    xvfb-run bash -c 'tauri-driver & pytest desktop/tests/e2e/test_app_linux.py'

WebKitGTK has no DevTools port, so these drive the window over WebDriver
instead. The same checks as the Windows tests, minus the job object, which
is Windows-only. Temp FLANNER_HOME and app folder, and Connect unticked,
so nothing on the machine is touched.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

APP = os.environ.get("FLANNER_DESKTOP_APP")
DATA = os.environ.get("FLANNER_DESKTOP_DATA")
DRIVER = os.environ.get("TAURI_DRIVER_URL", "http://127.0.0.1:4444")
pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="tauri-driver runs on Linux"),
    # tauri-driver starts the app with its own environment, so these are
    # exported before it starts, not set from here.
    pytest.mark.skipif(
        not (APP and DATA and os.environ.get("FLANNER_DESKTOP_RUNTIME")),
        reason="export FLANNER_DESKTOP_APP, FLANNER_DESKTOP_DATA and FLANNER_DESKTOP_RUNTIME "
        "(and a temp FLANNER_HOME) before starting tauri-driver",
    ),
]


@pytest.fixture(scope="module")
def data() -> Path:
    return Path(str(DATA))


@pytest.fixture(scope="module")
def driver() -> Iterator[Any]:
    webdriver = pytest.importorskip("selenium.webdriver")
    # Imported by name: newer selenium loads its submodules lazily, so
    # `webdriver.common.options` is no longer there as an attribute.
    from selenium.webdriver.common.options import ArgOptions

    options = ArgOptions()
    options.set_capability("browserName", "wry")
    options.set_capability("tauri:options", {"application": str(Path(str(APP)).resolve())})
    session = webdriver.Remote(command_executor=DRIVER, options=options)
    try:
        yield session
    finally:
        session.quit()


def _until(check: Any, seconds: float = 120) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        found = check()
        if found:
            return found
        assert time.monotonic() < deadline, "timed out"
        time.sleep(1)


def _invoke(driver: Any, command: str, args: dict[str, Any]) -> str:
    return str(
        driver.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            "window.__TAURI__.core.invoke(arguments[0], arguments[1])"
            ".then(() => done('allowed'), e => done(String(e)));",
            command,
            args,
        )
    )


def test_a_first_start_asks_with_connect_already_ticked(driver: Any) -> None:
    from selenium.webdriver.common.by import By

    _until(
        lambda: (
            driver.find_elements(By.ID, "connect")
            and driver.find_element(By.ID, "setup").is_displayed()
        )
    )
    assert driver.find_element(By.ID, "connect").is_selected()
    assert "PATH" in driver.find_element(By.ID, "changes").text


def test_the_answer_starts_flanner_and_is_remembered(driver: Any, data: Path) -> None:
    from selenium.webdriver.common.by import By

    driver.find_element(By.ID, "connect").click()
    driver.find_element(By.ID, "continue").click()
    _until(lambda: driver.current_url.startswith("http://127.0.0.1:"))
    _until(lambda: driver.title == "Dashboard")
    saved = json.loads((data / "settings.json").read_text(encoding="utf-8"))
    assert saved["flanner"] == "bundled"
    assert saved["connected"] is False
    assert saved["version"]  # remembered, so the next version can say it was updated


def test_the_page_may_open_the_folder_dialog(driver: Any) -> None:
    assert "invalid args" in _invoke(driver, "plugin:dialog|open", {"options": 123})


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("plugin:dialog|message", {"message": "x"}),
        ("finish_setup", {"connect": True, "keepInstalled": False}),
    ],
)
def test_the_page_may_do_nothing_else(driver: Any, command: str, args: dict[str, Any]) -> None:
    assert "not allowed" in _invoke(driver, command, args)
