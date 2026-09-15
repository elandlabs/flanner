"""The New project form can browse for its project root and plan directory.

Both fields took a typed path only. A browser will not give a page the real
path of a folder somebody picks, so the picker lists folders through the
local server and fills in the one chosen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"
WEB = Path(__file__).resolve().parent.parent / "flanner" / "web"


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    return TestClient(app, base_url=LOCAL_URL)


@pytest.fixture
def tree(tmp_path):
    """A folder with a repository, a plain folder, a nested plan folder and a file."""
    work = tmp_path / "work"
    (work / "billing" / ".git").mkdir(parents=True)
    (work / "billing" / "docs" / "plans").mkdir(parents=True)
    (work / "Archive").mkdir()
    (work / "notes.txt").write_text("not a folder", encoding="utf-8")
    return work


def test_a_folder_lists_its_folders_and_marks_repositories(client, tree):
    listed = client.get("/api/directories", params={"path": str(tree)}).json()

    assert listed["path"] == str(tree.resolve())
    assert [entry["name"] for entry in listed["entries"]] == ["Archive", "billing"]
    assert {entry["name"]: entry["git"] for entry in listed["entries"]} == {
        "Archive": False,
        "billing": True,
    }
    assert listed["parent"] == str(tree.resolve().parent)


def test_a_folder_under_the_base_is_given_relative_to_it(client, tree):
    root = tree / "billing"

    listed = client.get(
        "/api/directories", params={"path": str(root / "docs" / "plans"), "base": str(root)}
    ).json()
    outside = client.get(
        "/api/directories", params={"path": str(tree / "Archive"), "base": str(root)}
    ).json()

    assert listed["relative"] == "docs/plans"
    assert outside["relative"] is None


def test_a_path_that_is_not_a_folder_says_so(client, tree):
    assert (
        "is not a folder"
        in client.get("/api/directories", params={"path": str(tree / "notes.txt")}).json()["error"]
    )


def test_the_form_offers_to_browse_for_both_fields(client):
    page = client.get("/projects/new").text

    assert 'data-dir-picker="#project_root"' in page
    assert 'data-dir-picker="#plan_directory"' in page and 'data-dir-base="#project_root"' in page
    assert '<dialog id="dir-picker"' in page
    script = (WEB / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "[data-dir-picker]" in script and "/api/directories?" in script
