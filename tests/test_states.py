"""Error, warning, and flash-message states in the web UI."""

import pytest
from fastapi.testclient import TestClient

from flanner.server import create_plan_file_tool, create_project_tool
from flanner.web import app

# The Host header the middleware expects. TestClient defaults to
# "testserver", which flanner refuses on purpose: a Host it does not
# serve is how DNS rebinding reaches a local-only tool.
LOCAL_URL = "http://127.0.0.1:8080"

MISSING_UUID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def client(db):
    return TestClient(app, base_url=LOCAL_URL)


# --- error pages are HTML for browsers, JSON for the API --------------------


def test_404_renders_styled_page(client):
    r = client.get(f"/projects/{MISSING_UUID}")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "404 Not Found" in r.text
    assert "Back to Dashboard" in r.text


def test_400_bad_uuid_renders_styled_page(client):
    r = client.get("/plans/not-a-uuid")
    assert r.status_code == 400
    assert "400 Bad Request" in r.text


def test_invalid_query_param_renders_styled_400(client):
    # A page number falls back rather than failing (see flanner.paging), so
    # the typed parameter that still validates is freshness's ?full=.
    r = client.get("/freshness?full=abc")
    assert r.status_code == 400
    assert "invalid value" in r.text


def test_api_errors_stay_json(client):
    r = client.get(f"/api/projects/{MISSING_UUID}/plans")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["detail"]


def test_unhandled_exception_hides_traceback(client, monkeypatch):
    import flanner.web as webmod

    def boom(session, limit=None, offset=0):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(webmod, "db_list_projects", boom)
    quiet = TestClient(app, base_url=LOCAL_URL, raise_server_exceptions=False)
    r = quiet.get("/projects")
    assert r.status_code == 500
    assert "500 Server Error" in r.text
    assert "secret internals" not in r.text  # traceback never leaks


# --- flash messages -----------------------------------------------------------


def test_delete_shows_success_message(client, git_repo):
    project = create_project_tool(
        name="to-delete", project_root=str(git_repo), plan_directory=".plans"
    )
    r = client.post(f"/projects/{project['id']}/delete", follow_redirects=True)
    assert r.status_code == 200
    assert "Project deleted." in r.text


def test_no_changes_edit_shows_info_message(client, git_repo):
    project = create_project_tool(
        name="no-change", project_root=str(git_repo), plan_directory=".plans"
    )
    plan = create_plan_file_tool(
        project_id=project["id"], name="p", content="# Same", created_by="t"
    )
    # posting identical content redirects with message=no_changes
    r = client.post(f"/plans/{plan['id']}/edit", data={"content": "# Same"}, follow_redirects=True)
    assert r.status_code == 200
    assert "a new version was not created" in r.text
