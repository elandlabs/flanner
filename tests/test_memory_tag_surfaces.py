"""Tags where an agent, a terminal and a browser meet them.

The rules live in `memory_ops` and are tested in test_memory_tags.py. This
file checks that each surface passes tags through and reads them back, so a
tag set in one place is the tag found in the others.
"""

from __future__ import annotations

import json
import subprocess
from urllib.parse import unquote
from uuid import UUID

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from flanner import memory_ops, server
from flanner.cli import cli
from flanner.database import get_memory, get_session

LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An adopted repository, with the process standing in it."""
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


def _run(runner, *args):
    result = runner.invoke(cli, list(args))
    assert result.exit_code == 0, result.output
    return result


def _tags(memory_id: str) -> list[str]:
    session = get_session()
    session.expire_all()
    return memory_ops.tags_of(get_memory(session, UUID(memory_id)))


# --- the agent ---------------------------------------------------------------


def test_an_agent_saves_tags_and_finds_by_them(repo):
    saved = server.memory_remember("Tokens rotate daily.", "fact", tags=["Auth"])
    server.memory_remember("The login page is server-rendered.", "fact", tags=["ui"])

    assert saved["tags"] == ["auth"]
    recalled = server.memory_recall("tokens", tags=["auth"])
    assert [m["id"] for m in recalled["memories"]] == [saved["id"]]
    listed = server.memory_list(tags=["auth"])
    assert [m["id"] for m in listed["memories"]] == [saved["id"]]
    assert listed["memories"][0]["tags"] == ["auth"]


def test_an_agent_retags_without_a_new_version(repo):
    saved = server.memory_remember("Billing runs nightly.", "fact", tags=["billing"])

    changed = server.memory_tag(saved["id"], add=["jobs"], remove=["billing"])

    assert changed["changed"] is True and changed["id"] == saved["id"]
    assert _tags(saved["id"]) == ["jobs"]
    assert server.memory_get(saved["id"])["events"][-1]["action"] == "retagged"


def test_a_bad_tag_is_an_answer_not_a_crash(repo):
    saved = server.memory_remember("Something.", "fact")

    assert server.memory_tag(saved["id"], add=["not;ok"])["error"] is True
    assert server.memory_recall("x", tags=["not;ok"])["error"] is True
    assert server.memory_list(tags=["not;ok"])["error"] is True
    assert server.memory_remember("Other.", "fact", tags=["not;ok"])["error"] is True


def test_an_agent_sees_the_tags_in_use_and_what_is_related(repo):
    first = server.memory_remember("Auth uses JWT.", "decision", tags=["auth"])
    second = server.memory_remember("Pen test yearly.", "fact", tags=["auth"])

    assert server.memory_tags()["tags"] == [{"tag": "auth", "count": 2}]
    detail = server.memory_get(first["id"], related=True)
    assert [r["id"] for r in detail["related"]] == [second["id"]]
    assert "related" not in server.memory_get(first["id"])


def test_a_suggestion_from_an_agent_keeps_its_tags(repo):
    result = server.memory_consider(
        [{"content": "The queue is at-least-once.", "category": "fact", "tags": ["queue"]}]
    )

    [outcome] = result["outcomes"]
    assert _tags(outcome["id"]) == ["queue"]


# --- the terminal ------------------------------------------------------------


def test_the_terminal_tags_lists_and_shows(repo):
    runner = repo
    _run(
        runner,
        "mem",
        "remember",
        "Use advisory locks.",
        "--category",
        "decision",
        "--tag",
        "db",
        "--tag",
        "Locks",
    )
    _run(runner, "mem", "remember", "Use UTC.", "--category", "decision", "--tag", "db")
    listed = json.loads(_run(runner, "mem", "list", "--tag", "locks", "--output", "json").output)
    [locks] = listed
    assert locks["tags"] == ["db", "locks"]

    _run(runner, "mem", "tag", locks["id"], "infra", "--remove", "locks")
    assert _tags(locks["id"]) == ["db", "infra"]

    counted = json.loads(_run(runner, "mem", "tags", "--output", "json").output)
    assert counted[0] == {"tag": "db", "count": 2}

    shown = _run(runner, "mem", "show", locks["id"], "--related").output
    assert "db, infra" in shown
    assert "Use UTC." in shown and "shares tags: db" in shown

    found = json.loads(
        _run(runner, "mem", "recall", "UTC", "--tag", "db", "--output", "json").output
    )
    assert [m["title"] for m in found["memories"]] == ["Use UTC."]


def test_tagging_with_nothing_to_do_says_so(repo):
    runner = repo
    _run(runner, "mem", "remember", "Use UTC.", "--category", "decision")
    [memory] = json.loads(_run(runner, "mem", "list", "--output", "json").output)

    refused = runner.invoke(cli, ["mem", "tag", memory["id"]])

    assert refused.exit_code == 1
    assert "Name a tag" in refused.output


# --- the browser -------------------------------------------------------------


@pytest.fixture
def client(repo):
    from flanner.web import app

    return TestClient(app, base_url=LOCAL_URL, follow_redirects=False)


def _landed_on(response) -> str:
    return unquote(response.headers["location"]).split("?")[0].rsplit("/", 1)[-1]


def test_the_page_saves_tags_and_filters_by_one(client):
    sent = client.post(
        "/memory/new",
        data={
            "content": "Deploys go out on Tuesdays.",
            "category": "decision",
            "scope": "project",
            "tags": "Release, ops",
        },
    )
    memory_id = _landed_on(sent)
    server.memory_remember("Unrelated.", "fact")

    assert _tags(memory_id) == ["release", "ops"]
    filtered = client.get("/memory?tag=release")
    assert "Deploys go out on Tuesdays." in filtered.text
    assert "Unrelated." not in filtered.text
    assert "Show every tag" in filtered.text
    assert "Unrelated." in client.get("/memory").text


def test_the_detail_page_adds_and_removes_tags_and_lists_related(client):
    first = server.memory_remember("Auth uses JWT.", "decision", tags=["auth"])
    server.memory_remember("Pen test yearly.", "fact", tags=["auth"])

    added = client.post(f"/memory/{first['id']}/tags", data={"add": "security, Auth"})
    assert added.status_code == 303
    assert _tags(first["id"]) == ["auth", "security"]

    page = client.get(f"/memory/{first['id']}")
    assert 'aria-label="Remove the tag security"' in page.text
    assert "Pen test yearly." in page.text and "shares tags: auth" in page.text

    client.post(f"/memory/{first['id']}/tags", data={"remove": "security"})
    assert _tags(first["id"]) == ["auth"]


def test_a_refused_tag_is_explained_on_the_page(client):
    saved = server.memory_remember("Something.", "fact")

    refused = client.post(f"/memory/{saved['id']}/tags", data={"add": "not;ok"})

    assert "said=" in refused.headers["location"]
    assert _tags(saved["id"]) == []
