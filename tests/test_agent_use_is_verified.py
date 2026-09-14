"""Registered is not the same as connected.

"Registered" only ever meant a config file named flanner. An agent that was
registered and never got through looked exactly like one that worked. The
MCP server now records which client made each call, and every surface that
lists agents says whether a call has arrived.
"""

from __future__ import annotations

import html
import logging
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from flanner import observe, setup_check
from flanner.cli import cli
from flanner.utils import utcnow
from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def log(tmp_path, monkeypatch):
    """A tool log of this test's own. The logger is cached, so it is rebuilt."""
    path = tmp_path / "mcp.log"
    monkeypatch.setenv(observe.LOG_PATH_ENV, str(path))
    monkeypatch.setattr(observe, "_tool_logger", None)
    yield path
    for handler in logging.getLogger("flanner.mcp").handlers:
        handler.close()


@pytest.fixture
def repo(tmp_path, monkeypatch, log):
    """An adopted repository, with the web UI and the CLI both standing in it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLANNER_HOME", str(home))
    where = tmp_path / "repo"
    where.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=where, check=True)  # noqa: S603,S607
    monkeypatch.chdir(where)
    runner = CliRunner()
    started = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(where)], input="demo\n"
    )
    assert started.exit_code == 0, started.output
    client = TestClient(app, base_url=LOCAL_URL, follow_redirects=False)
    return SimpleNamespace(runner=runner, where=where, client=client, init=started.output)


# --- which agent called, and when -------------------------------------------------


def test_the_log_records_which_client_called_and_reads_it_back(log):
    observe.tool_call("list_projects", ms=2.0, ok=True, client="codex-mcp-client")
    observe.tool_call("list_projects", ms=2.0, ok=True)

    seen = observe.last_calls()

    assert list(seen) == ["codex-mcp-client"]
    assert abs((utcnow() - seen["codex-mcp-client"]).total_seconds()) < 60


def test_each_host_name_is_matched_to_its_agent(log):
    for client in ("claude-code", "codex-mcp-client", "claude-ai"):
        observe.tool_call("list_projects", ms=1.0, ok=True, client=client)

    used = setup_check.last_used()

    assert set(used) == {"claude_code", "codex", "claude_desktop"}
    assert used["codex"] == "just now"


def test_a_client_that_is_not_a_known_agent_is_not_credited_to_one(log):
    observe.tool_call("list_projects", ms=1.0, ok=True, client="some-other-tool")

    assert setup_check.last_used() == {}


def test_outside_a_request_the_server_names_no_client():
    from flanner.server import _client_name

    assert _client_name() == ""


# --- what a person is told ---------------------------------------------------------


def test_registered_but_never_called_says_so_and_gives_the_prompt(repo):
    """`init` registers Claude Code for the repository, and nothing has called."""
    # Collapsed, because the template wraps its sentences across lines.
    page = " ".join(html.unescape(repo.client.get("/settings").text).split())
    status = repo.runner.invoke(cli, ["status"]).output

    assert "no call has reached flanner yet" in page
    assert "list my flanner projects" in page
    assert "not yet used" in page
    assert "no call yet" in status


def test_a_call_in_the_log_shows_the_agent_as_connected(repo):
    observe.tool_call("list_projects", ms=1.0, ok=True, client="claude-code")

    page = " ".join(html.unescape(repo.client.get("/setup").text).split())
    status = repo.runner.invoke(cli, ["status"]).output

    assert "last call just now" in page and "connected" in page
    assert "last call" in status


def test_init_ends_on_something_to_try(repo):
    assert "list my flanner projects" in repo.init
