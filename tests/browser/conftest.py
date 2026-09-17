"""One server, one seeded catalog, one browser context — for the whole session.

The server is a subprocess rather than an ASGI transport because the point
of this suite is the JavaScript, and the JavaScript needs a real origin:
`fetch`, `EventSource` and `history.pushState` all do.

Its data is a `FLANNER_HOME` this session owns. `flanner/database.py` keeps a
module-level engine, so two suites pointed at one home would see each other's
projects; a home per session is the cheapest isolation that actually holds,
and it also makes `-n` safe because each worker starts its own server.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright.sync_api", reason="the browser suite needs playwright")

from playwright.sync_api import ConsoleMessage, Page  # noqa: E402

from tests.browser.pages.shell import Shell  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: How long the server gets to answer before the suite gives up on it. Generous:
#: a cold import of fastapi plus sqlalchemy on a loaded CI runner is seconds.
SERVER_START_TIMEOUT = 60.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Everything under this directory is a browser test, and nothing else is.

    Marking each test by hand is a rule somebody eventually forgets, and the
    cost of forgetting is a browser test running in the job that promised not
    to need a browser.

    The path check is not decoration. pytest hands this hook the whole
    session's items even though the hook lives in a subdirectory's conftest,
    so marking them all would put the marker on every test in the repository
    — and `pytest -m browser` would then try to run the lot in a browser job.
    """
    here = Path(__file__).resolve().parent
    for item in items:
        if here in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.browser)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def seeded_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A known catalog, built by `flanner demo seed` through the domain layer."""
    home = tmp_path_factory.mktemp("flanner-home")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    env.pop("FLANNER_DB_PATH", None)
    result = subprocess.run(  # noqa: S603
        # `-m flanner`, not `-m flanner.cli`: `cli.py` carries its own
        # `if __name__ == "__main__"` two thirds of the way down the file,
        # so running the module directly dispatches before the commands
        # below that line have been defined.
        [sys.executable, "-m", "flanner", "demo", "seed", "--home", str(home)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"seeding failed:\n{result.stdout}\n{result.stderr}")
    return home


@pytest.fixture(scope="session")
def server(seeded_home: Path) -> Iterator[str]:
    """uvicorn on 127.0.0.1, on a port nobody else has, over the seeded home.

    127.0.0.1 and not localhost: `flanner/web.py` refuses a `Host` header it
    does not serve, and the loopback literal is always on that list.
    """
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "FLANNER_HOME": str(seeded_home),
        # The suite asserts on the UI, not on pypi being reachable, and a
        # release check on a runner with no network is a slow page.
        "FLANNER_NO_UPDATE_CHECK": "1",
    }
    env.pop("FLANNER_DB_PATH", None)
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "flanner.web:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_answering(process, base_url)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


def _wait_until_answering(process: subprocess.Popen[str], base_url: str) -> None:
    """Poll the dashboard until it answers, or say why it never will."""
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"the server exited with {process.returncode}:\n{output}")
        try:
            with urllib.request.urlopen(base_url + "/", timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            # Not up yet. The loop is the wait; there is no condition to
            # subscribe to on a process that has not bound its socket.
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError(f"the server did not answer on {base_url} within {SERVER_START_TIMEOUT}s")


@pytest.fixture(scope="session")
def catalog(seeded_home: Path) -> dict[str, Any]:
    """What the seeder made: ids by name.

    Ids are uuid4 column defaults, so they cannot be pinned through the
    domain layer. The catalog is addressed by name through this, never by a
    uuid written into a test.
    """
    from flanner.demo import load_manifest

    return load_manifest(seeded_home)


@pytest.fixture
def shell(page: Page) -> Shell:
    return Shell(page)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any], server: str) -> dict[str, Any]:
    """`reduced_motion` is not a nicety.

    `shell.css` collapses every animation under `prefers-reduced-motion`,
    including the navigation progress bar and the button spinner, which are
    infinite. Without this any wait for the page to settle waits forever.
    """
    return {
        **browser_context_args,
        "base_url": server,
        "reduced_motion": "reduce",
        "viewport": {"width": 1280, "height": 900},
    }


@pytest.fixture(autouse=True)
def fail_on_console_error(page: Page) -> Iterator[None]:
    """A page that logs an exception is broken even when the assertion passed."""
    logged: list[str] = []

    def note(message: ConsoleMessage) -> None:
        if message.type != "error":
            return
        # Chromium logs an HTTP status for the document itself as a console
        # error. That is the response code, not a page that broke: the
        # not-found journey asks for a 404 on purpose. A 404 on anything
        # else — a stylesheet, a script, an icon — is still a failure.
        if message.text.startswith("Failed to load resource") and (
            message.location.get("url") == page.url
        ):
            return
        logged.append(f"console.error: {message.text}")

    page.on("console", note)
    page.on("pageerror", lambda error: logged.append(f"pageerror: {error}"))
    yield
    assert not logged, "the page logged errors:\n" + "\n".join(logged)
