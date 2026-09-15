"""Deleting takes a held press, and a memory takes a file from the page.

Deleting a project asked through confirm(), a box people dismiss by reflex,
and deleting recorded skill use asked nothing at all. Both now fill while
held and act only when the fill completes. Separately, a memory's files
could be attached only from the command line or an agent; the memory page
now takes an upload, through the same service, and removes one.
"""

from __future__ import annotations

import html
import subprocess
from pathlib import Path
from urllib.parse import unquote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from flanner import actions, memory_ops
from flanner.database import create_project, get_session
from flanner.services import dispatch
from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"
WEB = Path(__file__).resolve().parent.parent / "flanner" / "web"


@pytest.fixture
def memory(db, tmp_path, monkeypatch):
    """A project memory, and a client standing in its repository."""
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    monkeypatch.chdir(root)
    session = get_session()
    project = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    memory_id = dispatch(
        "memory_remember",
        {
            "content": "Deploys go out on Tuesdays.",
            "category": "fact",
            "project_id": str(project.id),
        },
        surface=actions.CLI,
    )["id"]
    client = TestClient(app, base_url=LOCAL_URL, follow_redirects=False)
    return client, memory_id, project


def attached(memory_id: str) -> list[dict]:
    return memory_ops.attachments_of(get_session(), UUID(memory_id))


# --- hold to delete ----------------------------------------------------------------


def test_deleting_a_project_is_held_rather_than_confirmed(memory):
    client, _, project = memory

    page = client.get(f"/projects/{project.id}").text

    assert "data-hold-confirm" in page
    assert "confirm(" not in page


def test_deleting_recorded_skill_use_is_held():
    source = (WEB / "templates" / "skills.html").read_text(encoding="utf-8")

    purge = source[source.index('action="/skills/purge"') :]
    purge = purge[: purge.index("</form>")]
    assert "data-hold-confirm" in purge


def test_the_hold_and_the_drop_zone_have_their_script_and_style():
    script = (WEB / "static" / "js" / "app.js").read_text(encoding="utf-8")
    css = (WEB / "static" / "css" / "shell.css").read_text(encoding="utf-8")

    assert "[data-hold-confirm]" in script and "requestSubmit" in script
    assert ".hold-confirm.is-holding::before" in css
