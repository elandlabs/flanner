"""The action history as a person meets it: the command line and the web page.

What an agent asked for has to be findable and decidable where a person
already works, under the same id the agent was given.
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from flanner import actions
from flanner.cli import cli
from flanner.database import create_project, get_memory, get_session
from flanner.services import dispatch
from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def asked(db, tmp_path, monkeypatch):
    """A forgotten memory, and an agent's pending request to bring it back."""
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    monkeypatch.chdir(root)
    session = get_session()
    proj = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    memory_id = dispatch(
        "memory_remember",
        {"content": "Deploys go out on Tuesdays.", "category": "fact", "project_id": str(proj.id)},
        surface=actions.CLI,
    )["id"]
    dispatch("memory_forget", {"memory_id": memory_id}, surface=actions.CLI)
    request = dispatch(
        "request_action",
        {"operation": "memory_restore", "arguments": {"memory_id": memory_id}},
        surface=actions.AGENT,
    )
    return session, memory_id, request["id"]


def restored(session, memory_id: str) -> bool:
    session.expire_all()
    return get_memory(session, uuid.UUID(memory_id)).status == "active"


def test_the_cli_lists_previews_and_applies_what_an_agent_asked_for(asked):
    session, memory_id, action_id = asked
    runner = CliRunner()

    listed = runner.invoke(cli, ["actions", "list", "--pending", "--output", "json"])
    shown = runner.invoke(cli, ["actions", "show", action_id[:8]])
    applied = runner.invoke(cli, ["actions", "apply", action_id[:8]])
    after = runner.invoke(cli, ["actions", "list", "--output", "json"])

    assert [row["id"] for row in json.loads(listed.output)] == [action_id]
    assert shown.exit_code == 0 and "Bring back the memory" in shown.output
    assert applied.exit_code == 0, applied.output
    decided = next(row for row in json.loads(after.output) if row["id"] == action_id)
    assert (decided["state"], decided["decided_surface"]) == ("applied", "cli")
    assert restored(session, memory_id)


def test_the_cli_declines_without_changing_anything(asked):
    session, memory_id, action_id = asked

    declined = CliRunner().invoke(cli, ["actions", "decline", action_id])

    assert declined.exit_code == 0, declined.output
    assert not restored(session, memory_id)


def test_the_web_page_shows_a_request_and_applies_it_under_the_same_id(asked):
    session, memory_id, action_id = asked
    client = TestClient(app, base_url=LOCAL_URL, follow_redirects=False)

    page = client.get("/actions")
    decided = client.post(f"/actions/{action_id}/decide", data={"decision": "apply"})

    assert page.status_code == 200
    assert "Bring back the memory" in page.text and action_id in page.text
    assert decided.status_code == 303
    row = actions.view(actions.get(session, action_id))
    assert (row["state"], row["decided_surface"]) == ("applied", "web")
    assert restored(session, memory_id)
