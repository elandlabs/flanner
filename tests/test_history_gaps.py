"""The action history covers writes that do not go through the service layer.

`flanner skills rollback` and the web UI's skill buttons call the domain
directly, so they left no record, and the history missed exactly the writes
a person makes by hand. Each surface now records them at one point, and
skips any write dispatch already recorded, so nothing appears twice.
"""

from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from flanner import actions
from flanner.cli import cli
from flanner.database import get_session
from flanner.services import dispatch

LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An adopted repository, as the command line sees one."""
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
    return runner


def history() -> list[dict]:
    """Newest first."""
    return [actions.view(row) for row in actions.recent(get_session(), limit=0)]


def test_a_cli_write_outside_the_service_layer_is_recorded(repo):
    before = len(history())

    ran = repo.invoke(cli, ["mem", "mode", "off"])

    assert ran.exit_code == 0, ran.output
    after = history()
    assert len(after) == before + 1
    latest = after[0]
    assert (latest["surface"], latest["operation"], latest["state"]) == ("cli", "mem mode", "done")
    assert latest["subject"] == actions.written_by(actions.CLI, "mem mode").action


def test_a_cli_write_through_the_service_layer_is_recorded_once(repo):
    """`mem restore` writes through dispatch, which records it with its arguments."""
    memory = dispatch(
        "memory_remember",
        {"content": "Use advisory locks.", "category": "decision"},
        surface="cli",
    )
    dispatch("memory_forget", {"memory_id": memory["id"]}, surface="cli")
    before = len(history())

    ran = repo.invoke(cli, ["mem", "restore", memory["id"]])

    assert ran.exit_code == 0, ran.output
    after = history()
    assert len(after) == before + 1
    assert after[0]["operation"] == "memory_restore"


def test_a_refused_cli_write_is_recorded_as_failed(repo):
    ran = repo.invoke(cli, ["skills", "rollback", "not-an-installation"])

    assert ran.exit_code == 1
    latest = history()[0]
    assert (latest["operation"], latest["state"]) == ("skills rollback", "failed")


def test_reads_and_agent_hooks_leave_no_record(repo):
    before = len(history())

    repo.invoke(cli, ["mem", "list"])
    repo.invoke(cli, ["hook", "guard-write"], input="{}")

    assert len(history()) == before


@pytest.fixture
def client(repo):
    from flanner.web import app

    return TestClient(app, base_url=LOCAL_URL, follow_redirects=False)


def test_a_web_skill_button_is_recorded_with_its_refusal(client):
    before = len(history())

    answered = client.post("/skills/rollback", data={"installation_id": "not-an-installation"})

    assert answered.status_code == 303
    after = history()
    assert len(after) == before + 1
    latest = after[0]
    assert (latest["surface"], latest["operation"], latest["state"]) == (
        "web",
        "POST /skills/rollback",
        "failed",
    )
    assert latest["message"]


def test_a_web_decision_on_a_request_is_not_recorded_twice(client):
    memory = dispatch(
        "memory_remember",
        {"content": "Deploys go out on Tuesdays.", "category": "fact"},
        surface="cli",
    )
    dispatch("memory_forget", {"memory_id": memory["id"]}, surface="cli")
    asked = dispatch(
        "request_action",
        {"operation": "memory_restore", "arguments": {"memory_id": memory["id"]}},
        surface="agent",
    )
    before = len(history())

    client.post(f"/actions/{asked['id']}/decide", data={"decision": "decline"})

    assert len(history()) == before
    assert history()[0]["state"] == actions.DECLINED
