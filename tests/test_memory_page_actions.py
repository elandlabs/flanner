"""The memory pages do what the CLI does: save, correct, forget, restore, filter, sort.

The page could read, share and attach, and nothing else. Saving, correcting,
forgetting and restoring were terminal-only, and the list could be searched
but not filtered or sorted.
"""

from __future__ import annotations

import html
import subprocess
from urllib.parse import unquote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from flanner import actions
from flanner.database import create_project, get_memory, get_session
from flanner.services import dispatch
from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def home(db, tmp_path, monkeypatch):
    """A project, and a client standing in its repository."""
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    monkeypatch.chdir(root)
    project = create_project(
        get_session(), name="demo", project_root=str(root), auto_gitignore=False
    )
    return TestClient(app, base_url=LOCAL_URL, follow_redirects=False), project


def remember(project, content: str, category: str = "fact") -> str:
    result = dispatch(
        "memory_remember",
        {"content": content, "category": category, "project_id": str(project.id)},
        surface=actions.CLI,
    )
    return str(result["id"])


def status_of(memory_id: str) -> str:
    session = get_session()
    session.expire_all()
    return get_memory(session, UUID(memory_id)).status


def landed_on(response) -> str:
    return unquote(response.headers["location"]).split("?")[0].rsplit("/", 1)[-1]


def test_a_memory_can_be_saved_from_the_page(home):
    client, _ = home

    sent = client.post(
        "/memory/new",
        data={
            "content": "Deploys go out on Tuesdays.",
            "category": "decision",
            "scope": "project",
        },
    )

    assert sent.status_code == 303
    saved = get_memory(get_session(), UUID(landed_on(sent)))
    assert saved.body.startswith("Deploys go out on Tuesdays.")
    assert (saved.category, saved.scope, saved.created_by) == ("decision", "project", "web")


def test_forgetting_hides_a_memory_and_restoring_brings_it_back(home):
    client, project = home
    memory_id = remember(project, "The staging database is rebuilt nightly.")

    page = client.get(f"/memory/{memory_id}").text
    assert 'action="/memory/' + memory_id + '/forget"' in page and "data-hold-confirm" in page

    forgot = client.post(f"/memory/{memory_id}/forget")
    assert status_of(memory_id) == "forgotten"
    # One notice, from the page, rather than the same sentence twice.
    assert "said=" not in forgot.headers["location"]
    assert client.get(forgot.headers["location"]).text.count('class="notice') == 1
    assert "staging database" not in client.get("/memory").text
    assert "staging database" in client.get("/memory?status=forgotten").text
    assert "/restore" in client.get(f"/memory/{memory_id}").text

    client.post(f"/memory/{memory_id}/restore")
    assert status_of(memory_id) == "active"


def test_correcting_saves_a_new_version_and_keeps_the_old(home):
    client, project = home
    memory_id = remember(project, "Releases are cut on Mondays.", "decision")

    sent = client.post(
        f"/memory/{memory_id}/supersede",
        data={"content": "Releases are cut on Tuesdays.", "reason": "Monday is support day"},
    )

    replacement = landed_on(sent)
    assert replacement != memory_id
    assert status_of(memory_id) == "superseded"
    assert get_memory(get_session(), UUID(replacement)).body.startswith(
        "Releases are cut on Tuesdays."
    )


def test_the_list_filters_by_category_and_sorts_by_title(home):
    client, project = home
    remember(project, "Charlie runs the load tests.", "lesson")
    remember(project, "Alpha is the default region.", "decision")
    remember(project, "Bravo is the fallback region.", "fact")

    facts = html.unescape(client.get("/memory?category=fact").text)
    by_title = html.unescape(client.get("/memory?sort=title").text)
    unknown = html.unescape(client.get("/memory?category=nonsense").text)

    assert "Bravo" in facts and "Alpha" not in facts and "Charlie" not in facts
    assert by_title.index("Alpha") < by_title.index("Bravo") < by_title.index("Charlie")
    assert all(name in unknown for name in ("Alpha", "Bravo", "Charlie"))
