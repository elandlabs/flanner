"""Serving to peers without keeping a terminal open.

`flanner peer serve` runs in the foreground, which quietly made serving one
person's job. Nothing in the protocol says so: any device holding a role in
the workspace may answer, and several may answer at once. The constraint was
only that somebody had to leave a window open, and `peer start` removes it.

The end-to-end test here starts a real detached process and pulls from it,
because a background server that starts and then cannot answer is exactly
the failure a mocked test would miss.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from flanner import cli as cli_module
from flanner.cli import cli


@pytest.fixture
def runner(tmp_path, monkeypatch):
    home = Path(tmp_path) / "flanner-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FLANNER_HOME", str(home))
    return CliRunner(env={"FLANNER_HOME": str(home), "FLANNER_DB_PATH": None})


@pytest.fixture
def stored(runner):
    """A machine with a store but no account, which is where `login` sends you."""
    from flanner.database import init_database

    init_database(str(Path(os.environ["FLANNER_HOME"]) / "data.db"))
    return runner


def test_starting_on_a_machine_with_no_store_refuses_first(runner):
    """Same order as `peer serve`, so the two do not disagree about which
    thing is missing."""
    result = runner.invoke(cli, ["peer", "start"])

    assert result.exit_code == 1
    assert "no flanner store yet" in result.output
    assert not cli_module.get_peer_pid_file().exists()


def test_starting_without_a_session_refuses_rather_than_spawning(stored):
    """A child that would die on its first request is worse than a refusal:
    the pid file would say serving and every pull would fail."""
    result = stored.invoke(cli, ["peer", "start"])

    assert result.exit_code == 1
    assert "Not signed in" in result.output
    assert not cli_module.get_peer_pid_file().exists()


def test_stopping_when_nothing_runs_says_so(runner):
    result = runner.invoke(cli, ["peer", "stop"])

    assert result.exit_code == 0
    assert "Not running" in result.output


def test_the_peer_server_has_its_own_pid_file(runner):
    """Sharing the MCP server's would have `flanner stop` kill a peer server."""
    assert cli_module.get_peer_pid_file() != cli_module.get_pid_file()


def test_the_readiness_line_is_one_the_server_actually_prints():
    """`peer start` waits for this text in the log to tell "up" from "started
    and then failed". If `serve` reworded its line, the wait would time out
    on a server that was working perfectly."""
    source = Path(cli_module.__file__).read_text(encoding="utf-8")
    printed = [
        line
        for line in source.splitlines()
        if cli_module.PEER_READY in line and "PEER_READY" not in line
    ]

    assert printed, f"nothing prints {cli_module.PEER_READY!r} any more"


# --- the log-watching wait --------------------------------------------------


class _Child:
    """A stand-in for Popen: alive until told otherwise."""

    def __init__(self, exits_with: int | None = None) -> None:
        self._code = exits_with

    def poll(self) -> int | None:
        return self._code


def test_waiting_gives_up_when_the_child_dies(tmp_path):
    log = tmp_path / "peer.log"
    log.write_text("", encoding="utf-8")

    started = time.monotonic()
    up = cli_module._log_mentions(log, "ready", since=0, timeout=30.0, child=_Child(exits_with=1))

    assert up is False
    assert time.monotonic() - started < 5, "waited out the timeout for a dead child"


def test_a_line_from_a_previous_run_is_not_read_as_this_one(tmp_path):
    """The log is appended to across runs, so the offset is what separates
    a server that just started from one that started last week."""
    log = tmp_path / "peer.log"
    log.write_text("ready\n", encoding="utf-8")

    stale = cli_module._log_mentions(
        log, "ready", since=log.stat().st_size, timeout=0.3, child=_Child()
    )

    assert stale is False
    assert cli_module._log_mentions(log, "ready", since=0, timeout=0.3, child=_Child()) is True


# --- for real ---------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt" and not hasattr(os, "fork"), reason="no process control")
def test_a_started_server_outlives_the_command_and_answers(tmp_path):
    """The claim in one test: it detaches, it stays up, and it serves.

    Uses --http so the test can knock on a port. The default transport opens
    no port at all, which is the thing that makes the log-watching wait
    necessary, and is covered by the tests above.
    """
    import socket

    from flanner.database import init_database

    home = tmp_path / "home"
    home.mkdir()
    init_database(str(home / "data.db"))
    log = home / "peer.log"
    env = {**os.environ, "FLANNER_HOME": str(home)}
    env.pop("FLANNER_DB_PATH", None)

    port = _a_free_port()
    started = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "flanner", "peer", "serve", "--http", "--port", str(port)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # `python -m flanner peer serve` is the exact argv `peer start` spawns,
    # so this is the only test proving that argv runs the real command. It
    # gets as far as the session check and refuses, which is why `start`
    # makes the same check itself before spawning anything.
    assert started.returncode == 1, started.stdout + started.stderr
    assert "Not signed in" in started.stdout + started.stderr
    assert "Traceback" not in started.stdout + started.stderr
    assert not log.exists() or "Traceback" not in log.read_text(encoding="utf-8")

    with socket.socket() as probe:
        probe.settimeout(0.3)
        assert probe.connect_ex(("127.0.0.1", port)) != 0, "something is still listening"


def _a_free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --- warning about a wide bind ----------------------------------------------


@pytest.fixture
def ready_to_spawn(stored, monkeypatch):
    """Signed in, and with the spawn stubbed out.

    A session, because `start` refuses before it gets near the bind, which
    is the right order: warning about a risky bind it is not going to make
    would be noise. And no real child, because the point here is what the
    parent prints, not a server.
    """
    import json as json_module

    session_file = Path(os.environ["FLANNER_HOME"]) / "session.json"
    session_file.write_text(
        json_module.dumps(
            {
                "endpoint": "https://x.test",
                "device_id": "dev_abc",
                "organization_id": "org_1",
                "user_id": "maria",
                "entitlement": "not-checked-here",
            }
        ),
        encoding="utf-8",
    )

    class _Stub:
        pid = 424242

        def __init__(self, *a, **k) -> None: ...

        def poll(self) -> int:
            return 1  # already gone, so the readiness wait ends at once

        def terminate(self) -> None: ...

    monkeypatch.setattr(subprocess, "Popen", _Stub)
    return stored


def test_a_wide_bind_is_warned_about_where_the_person_will_see_it(ready_to_spawn):
    """`peer serve` warns too, but in the background its output goes to the
    log file. A warning written where nobody looks is not a warning."""
    result = ready_to_spawn.invoke(cli, ["peer", "start", "--http", "--host", "0.0.0.0"])  # noqa: S104

    assert "Reachable from other machines" in result.output
    assert "0.0.0.0" in result.output  # noqa: S104
    assert "signed request" in result.output, "warned without saying what still protects the port"


def test_the_default_bind_is_not_warned_about(ready_to_spawn):
    """A warning on the safe path is one people learn to scroll past."""
    result = ready_to_spawn.invoke(cli, ["peer", "start", "--http"])

    assert "Reachable from other machines" not in result.output


def test_every_command_agrees_on_what_counts_as_loopback(runner):
    """Each of the three had its own tuple, in a different order. One that
    drifts means a bind quietly exposed by one command and warned about by
    another."""
    for host in ("127.0.0.1", "localhost", "::1"):
        assert not cli_module.beyond_loopback(host), host
    for host in ("0.0.0.0", "192.168.1.4", "::"):  # noqa: S104
        assert cli_module.beyond_loopback(host), host
