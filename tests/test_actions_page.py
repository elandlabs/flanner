"""The action history page is paged and searchable, like every other list.

It showed the newest fifty actions and stopped, with no way to reach an
older one or to find a particular one.
"""

from __future__ import annotations

import html
import subprocess

import pytest
from fastapi.testclient import TestClient

from flanner import actions
from flanner.database import create_project, get_session
from flanner.services import dispatch
from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def history(db, tmp_path, monkeypatch):
    """Seventeen recorded memory writes, more than one page of fifteen."""
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    monkeypatch.chdir(root)
    session = get_session()
    project = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    ids = [
        dispatch(
            "memory_remember",
            {"content": f"Fact number {n}.", "category": "fact", "project_id": str(project.id)},
            surface=actions.CLI,
        )["action_id"]
        for n in range(17)
    ]
    return TestClient(app, base_url=LOCAL_URL), ids


def test_the_history_is_split_into_pages(history):
    client, _ = history

    first = html.unescape(client.get("/actions").text)
    second = html.unescape(client.get("/actions?page=2&per=15").text)

    assert "1–15 of 17" in first
    assert "16–17 of 17" in second


def test_search_covers_every_page(history):
    client, ids = history
    oldest = ids[0][:8]

    found = client.get(f"/actions?q={oldest}").text

    assert found.count("<code>") == 1 and oldest in found
    assert 'value="' + oldest + '"' in found


def test_a_search_with_no_match_says_so(history):
    client, _ = history

    page = html.unescape(client.get("/actions?q=nothing-like-this").text)

    assert "No recorded action matches “nothing-like-this”." in page


def test_the_id_column_is_written_id(history):
    client, _ = history

    page = client.get("/actions").text

    assert "<span>ID</span>" in page and "<span>Id</span>" not in page
