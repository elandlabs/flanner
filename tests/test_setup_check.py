"""One setup check, shown by `flanner status` and by the agent's context tool.

Checking a setup used to mean two commands and reconciling them by hand:
`status` knew about the server and the agents, `project_context` knew about
the project and memory. These tests hold the two to one answer.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest
from click.testing import CliRunner

from flanner import session as cache
from flanner import setup_check
from flanner.cli import cli
from flanner.database import create_project, get_session


@pytest.fixture
def adopted(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    monkeypatch.chdir(root)
    session = get_session()
    create_project(session, name="shop", project_root=str(root), auto_gitignore=False)
    return session


def test_the_check_names_everything_a_setup_question_needs(adopted):
    found = setup_check.check(adopted)

    assert set(found) == {"agents", "tools", "project", "capture_mode", "watching", "peers"}
    assert set(found["agents"]) == {"claude_desktop", "claude_code", "codex"}
    assert found["project"]["name"] == "shop"
    assert found["capture_mode"] == "suggest"
    assert found["watching"] == []
    assert found["peers"]["signed_in"] is False


def test_the_tools_it_counts_are_the_ones_the_server_advertises(adopted):
    from flanner import server

    advertised = {tool.name for tool in asyncio.run(server._mcp.list_tools())}

    assert set(setup_check.tools()) == advertised
    assert setup_check.check(adopted)["tools"]["count"] == len(advertised)


def test_peers_count_only_the_other_devices_this_machine_knows(adopted):
    cache.save(
        cache.Session(
            endpoint="https://api.example.test",
            device_id="dev_me",
            organization_id="org_1",
            user_id="usr_me",
            entitlement="claims.signature",
            device_keys={"dev_me": "k", "dev_laptop": "k", "dev_desk": "k"},
        )
    )

    peers = setup_check.peers()

    assert (peers["signed_in"], peers["user_id"], peers["known_devices"]) == (True, "usr_me", 2)


def test_status_prints_the_whole_check(adopted):
    output = CliRunner().invoke(cli, ["status"]).output

    for label in ("Tools", "Project", "Capture", "Watching", "Peers"):
        assert label in output, label
    assert "shop" in output and "suggest" in output


def test_the_agent_is_given_the_same_check(adopted):
    from flanner.server import project_context

    context = project_context()

    assert context["setup"]["project"] == {
        "name": "shop",
        "root": context["project"]["project_root"],
    }
    assert context["setup"]["tools"]["count"] == setup_check.check(adopted)["tools"]["count"]
