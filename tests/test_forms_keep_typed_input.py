"""A form that fails keeps what was typed.

Three forms threw typed work away when they failed: two new-project errors
came back empty, a plan edit whose save failed ended in a server error, and
a refused skill draft revision redirected its body away (that one is tested
in test_skills_learn.py).
"""

from __future__ import annotations

import html
import logging
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from flanner import observe
from flanner.cli import cli
from flanner.database import get_project_by_root, get_session
from flanner.exceptions import DatabaseError
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


def test_a_new_project_error_keeps_what_was_typed(repo, tmp_path):
    plain = tmp_path / "not-a-repository"
    plain.mkdir()

    page = repo.client.post(
        "/projects/new",
        data={
            "name": "kept-name",
            "description": "kept description",
            "project_root": str(plain),
            "plan_directory": "docs/plans",
        },
    )
    text = html.unescape(page.text)

    assert "is not a valid git repository" in text
    assert 'value="kept-name"' in text
    assert 'value="kept description"' in text
    assert 'value="docs/plans"' in text


def test_a_plan_edit_that_fails_to_save_keeps_the_text(repo, monkeypatch):
    import flanner.web as web
    from flanner.plan_ops import create_plan

    session = get_session()
    project = get_project_by_root(session, str(repo.where))
    plan_file, _ = create_plan(
        session, project=project, name="kept", content="# first", description="", created_by="user"
    )

    def locked(*args, **kwargs):
        raise DatabaseError("Timed out waiting for write lock")

    monkeypatch.setattr(web, "record_new_version", locked)

    page = repo.client.post(
        f"/plans/{plan_file.id}/edit",
        data={"content": "# my long edit", "notes": "why it changed"},
    )
    text = html.unescape(page.text)

    assert page.status_code == 409
    assert "Not saved" in text
    assert "# my long edit" in text
    assert "why it changed" in text
