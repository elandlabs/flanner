"""Smoke tests for a built desktop runtime (the desktop PRD, section 12, layer 2).

    python desktop/bundle/build_runtime.py --flanner build/dist/flanner-X.Y.Z-py3-none-any.whl
    FLANNER_RUNTIME=build/runtime pytest desktop/tests/smoke

Each test drives the bundle the way the app and the agents do: through the
bundled Python and the launchers `flanner desktop-link` writes. Home, the
flanner home and the agents' config folders are all a temp dir, PATH holds
only the launchers, git and the OS, and flanner is told to keep its device
key out of the real keychain, so nothing here touches this machine's setup.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

RUNTIME = os.environ.get("FLANNER_RUNTIME")
pytestmark = pytest.mark.skipif(not RUNTIME, reason="set FLANNER_RUNTIME to a built runtime")

WINDOWS = sys.platform == "win32"
EXE = ".exe" if WINDOWS else ""


@pytest.fixture(scope="module")
def runtime() -> Path:
    return Path(str(RUNTIME)).resolve()


@pytest.fixture(scope="module")
def python(runtime: Path) -> Path:
    return runtime / "python.exe" if WINDOWS else runtime / "bin" / "python3"


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("home")


@pytest.fixture(scope="module")
def env(home: Path) -> dict[str, str]:
    bin_dir = home / ".flanner" / "bin"
    base = (
        [str(Path(os.environ["SYSTEMROOT"]) / "System32"), os.environ["SYSTEMROOT"]]
        if WINDOWS
        else ["/usr/bin", "/bin"]
    )
    git = shutil.which("git")
    path = [str(bin_dir), *([str(Path(git).parent)] if git else []), *base]
    keep = {"SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC", "PATHEXT", "LANG"}
    kept = {key: value for key, value in os.environ.items() if key.upper() in keep}
    return {
        **kept,
        "PATH": os.pathsep.join(path),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "FLANNER_HOME": str(home / ".flanner"),
        "FLANNER_NO_KEYCHAIN": "1",
        "PYTHONUTF8": "1",
    }


def _run(
    argv: list[str | Path], env: dict[str, str], **kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the bundle under test, fixed argv
        [str(arg) for arg in argv],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )


@pytest.fixture(scope="module")
def launchers(python: Path, home: Path, env: dict[str, str]) -> Path:
    """What the app does after installing a runtime: link the launchers."""
    linked = _run([python, "-m", "flanner", "desktop-link"], env)
    assert linked.returncode == 0, linked.stderr
    bin_dir = home / ".flanner" / "bin"
    assert (bin_dir / f"flanner{EXE}").is_file()
    assert (bin_dir / f"flanner-mcp{EXE}").is_file()
    return bin_dir


def test_the_bundle_knows_it_is_the_desktop_app(launchers: Path, env: dict[str, str]) -> None:
    """Also proves build_runtime.MARKER still matches flanner.release.DESKTOP_MARKER."""
    shown = _run([launchers / f"flanner{EXE}", "--version"], env)
    assert shown.returncode == 0, shown.stderr
    assert "(desktop)" in shown.stdout


def test_the_users_own_packages_do_not_leak_in(
    python: Path, home: Path, env: dict[str, str]
) -> None:
    """A package in the user's site-packages must not shadow the bundle's."""
    user_site = _run([python, "-c", "import site; print(site.getusersitepackages())"], env)
    shadow = Path(user_site.stdout.strip())
    shadow.mkdir(parents=True, exist_ok=True)
    (shadow / "click.py").write_text("raise SystemExit('the user site leaked in')\n")
    try:
        imported = _run([python, "-c", "import click; print(click.__file__)"], env)
        assert imported.returncode == 0, imported.stderr
        assert str(shadow) not in imported.stdout
    finally:
        (shadow / "click.py").unlink()


def test_every_command_answers_help(python: Path, launchers: Path, env: dict[str, str]) -> None:
    listed = _run(
        [
            python,
            "-c",
            "from flanner.cli import cli;"
            "print('\\n'.join(n for n, c in cli.commands.items() if not c.hidden))",
        ],
        env,
    )
    names = listed.stdout.split()
    assert len(names) > 20, listed.stderr
    for name in names:
        helped = _run([launchers / f"flanner{EXE}", name, "--help"], env)
        assert helped.returncode == 0, f"flanner {name} --help: {helped.stderr}"


def test_init_points_claude_desktop_at_the_launcher(
    launchers: Path, home: Path, env: dict[str, str]
) -> None:
    repo = home / "repo"
    repo.mkdir()
    if shutil.which("git", path=env["PATH"]) is None:
        pytest.skip("git is not installed")
    assert _run(["git", "init", "-q", str(repo)], env).returncode == 0

    initialised = _run(
        [launchers / f"flanner{EXE}", "init", "--setup", "claude-desktop", "--no-watch-skills"],
        env,
        cwd=repo,
    )
    assert initialised.returncode == 0, initialised.stdout + initialised.stderr

    configs = list(home.rglob("claude_desktop_config.json"))
    assert len(configs) == 1, configs
    entry = json.loads(configs[0].read_text(encoding="utf-8"))["mcpServers"]["flanner"]
    assert entry["command"] == str(launchers / f"flanner-mcp{EXE}")
    project = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
    assert project["mcpServers"]["flanner"]["command"] == "flanner-mcp"


def test_an_agent_reaches_the_tools_through_the_launcher(
    launchers: Path, env: dict[str, str]
) -> None:
    """What Claude Code and Codex do: start flanner-mcp and talk over stdio."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def ask() -> tuple[set[str], bool]:
        params = StdioServerParameters(command=str(launchers / f"flanner-mcp{EXE}"), env=env)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            answer = await session.call_tool("list_projects", {})
            return tools, bool(answer.isError)

    tools, failed = asyncio.run(asyncio.wait_for(ask(), timeout=120))
    assert "create_plan_file_tool" in tools
    assert failed is False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def http_server(launchers: Path, env: dict[str, str]) -> Iterator[int]:
    port = _free_port()
    started = _run([launchers / f"flanner{EXE}", "start", "--port", str(port)], env)
    assert started.returncode == 0, started.stdout + started.stderr
    try:
        yield port
    finally:
        _run([launchers / f"flanner{EXE}", "stop"], env)


def test_flanner_starts_copies_of_itself_from_the_bundled_python(http_server: int) -> None:
    """`flanner start` runs sys.executable -m flanner.server; a frozen build could not."""
    deadline = time.monotonic() + 60
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{http_server}/mcp", timeout=2)  # noqa: S310
            break
        except urllib.error.HTTPError:
            break  # any HTTP answer means the server is up
        except OSError:
            if time.monotonic() > deadline:
                pytest.fail("the HTTP MCP server never answered")
            time.sleep(0.5)


def test_the_os_keychain_backend_is_found(python: Path, env: dict[str, str]) -> None:
    """keyring finds backends through entry points, which bundling can lose."""
    found = _run(
        [python, "-c", "import keyring; k = keyring.get_keyring(); print(type(k).__module__)"],
        env,
    )
    assert found.returncode == 0, found.stderr
    backend = found.stdout.strip()
    if WINDOWS:
        assert backend == "keyring.backends.Windows"
    elif sys.platform == "darwin":
        assert backend == "keyring.backends.macOS"
    else:
        # A headless runner has no Secret Service; flanner then uses its file.
        assert backend.startswith("keyring.backends.")


def test_the_peer_transport_loads(python: Path, env: dict[str, str]) -> None:
    loaded = _run([python, "-c", "import iroh"], env)
    assert loaded.returncode == 0, loaded.stderr
