"""Scale guardrails: pagination, render caps, MCP result limits."""

import uuid

import pytest
from fastapi.testclient import TestClient

import flanner.web as webmod
from flanner.database import PlanFileModel, ProjectModel, VersionModel, get_session
from flanner.paging import DEFAULT_PER_PAGE
from flanner.server import (
    create_plan_file_tool,
    create_project_tool,
    get_plan_file_tool,
    list_plan_files_tool,
)
from flanner.utils import utcnow
from flanner.web import app, render_plan_html

# The Host header the middleware expects. TestClient defaults to
# "testserver", which flanner refuses on purpose: a Host it does not
# serve is how DNS rebinding reaches a local-only tool.
LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def client(db):
    return TestClient(app, base_url=LOCAL_URL)


def _bulk_project(session, n_plans: int) -> ProjectModel:
    project = ProjectModel(name=f"bulk-{uuid.uuid4().hex[:8]}", plan_directory=".plans")
    session.add(project)
    session.flush()
    for i in range(n_plans):
        pf = PlanFileModel(project_id=project.id, name=f"plan-{i:04d}", current_version=1)
        session.add(pf)
        session.flush()
        session.add(
            VersionModel(
                plan_file_id=pf.id,
                version=1,
                file_path="/nonexistent.md",
                content_hash=f"h{i}",
                created_at=utcnow(),
            )
        )
    session.commit()
    return project


# --- pagination -------------------------------------------------------------


def test_project_detail_paginates(client):
    session = get_session()
    project = _bulk_project(session, 120)

    page1 = client.get(f"/projects/{project.id}")
    assert page1.status_code == 200
    assert "1–15 of 120" in page1.text and "page 1 of 8" in page1.text
    assert page1.text.count('class="plan-file-card"') == DEFAULT_PER_PAGE

    page8 = client.get(f"/projects/{project.id}?page=8")
    assert page8.status_code == 200
    assert "page 8 of 8" in page8.text

    # out-of-range pages clamp instead of erroring
    assert "page 8 of 8" in client.get(f"/projects/{project.id}?page=99").text
    assert "page 1 of 8" in client.get(f"/projects/{project.id}?page=-4").text

    # a bigger page from the menu, and a size off the menu is the default
    assert "page 1 of 3" in client.get(f"/projects/{project.id}?per=50").text
    assert "page 1 of 8" in client.get(f"/projects/{project.id}?per=7").text


def test_projects_list_paginates(client):
    session = get_session()
    for i in range(60):
        session.add(ProjectModel(name=f"proj-{i:03d}", plan_directory=".plans"))
    session.commit()

    page1 = client.get("/projects")
    assert page1.status_code == 200
    assert "1–15 of 60" in page1.text and "page 1 of 4" in page1.text
    page2 = client.get("/projects?page=2")
    assert "16–30 of 60" in page2.text and "page 2 of 4" in page2.text


def test_dashboard_caps_grid_and_links_to_all(client):
    session = get_session()
    for i in range(15):
        session.add(ProjectModel(name=f"dash-{i:03d}", plan_directory=".plans"))
    session.commit()

    r = client.get("/")
    assert r.status_code == 200
    assert "all 15 projects" in r.text


# --- render cap and cache ---------------------------------------------------


def test_small_plan_renders_markdown(client, tmp_path, git_repo):
    project = create_project_tool(
        name="render", project_root=str(git_repo), plan_directory=".plans"
    )
    plan = create_plan_file_tool(
        project_id=project["id"], name="small", content="# Heading\n\nbody", created_by="t"
    )
    r = client.get(f"/plans/{plan['id']}")
    assert r.status_code == 200
    assert "<h1" in r.text  # markdown actually rendered


def test_oversized_plan_served_raw_not_rendered(client, tmp_path, git_repo, monkeypatch):
    monkeypatch.setattr(webmod, "MAX_RENDER_CHARS", 1000)
    project = create_project_tool(
        name="render-big", project_root=str(git_repo), plan_directory=".plans"
    )
    plan = create_plan_file_tool(
        project_id=project["id"],
        name="big",
        content="# Big\n\n" + "word " * 500,
        created_by="t",
    )
    r = client.get(f"/plans/{plan['id']}")
    assert r.status_code == 200
    assert "rendering limit" in r.text
    assert 'class="raw-content"' in r.text
    assert 'class="markdown-content"' not in r.text  # markdown never converted


def test_render_cache_hits_by_content_hash(monkeypatch):
    calls = []
    monkeypatch.setattr(webmod, "markdown_filter", lambda c: calls.append(1) or "<p>x</p>")
    webmod._render_cache.clear()

    assert render_plan_html("content", "hash-a") == "<p>x</p>"
    assert render_plan_html("content", "hash-a") == "<p>x</p>"
    assert len(calls) == 1  # second call served from cache


# --- MCP result limits --------------------------------------------------------


def test_list_plan_files_tool_pages(db):
    session = get_session()
    project = _bulk_project(session, 75)

    first = list_plan_files_tool(str(project.id))
    assert len(first) == 50  # default page size

    rest = list_plan_files_tool(str(project.id), limit=50, offset=50)
    assert len(rest) == 25
    assert {f["id"] for f in first}.isdisjoint({f["id"] for f in rest})

    capped = list_plan_files_tool(str(project.id), limit=10_000)
    assert len(capped) == 75  # requested cap is clamped to 200, all 75 fit


def test_get_plan_file_tool_truncates(db, git_repo):
    project = create_project_tool(
        name="trunc", project_root=str(git_repo), plan_directory=".plans"
    )
    plan = create_plan_file_tool(
        project_id=project["id"], name="long", content="x" * 5000, created_by="t"
    )

    full = get_plan_file_tool(plan["id"])
    assert "truncated" not in full and len(full["content"]) == 5000

    cut = get_plan_file_tool(plan["id"], max_chars=1000)
    assert cut["truncated"] is True
    assert cut["total_chars"] == 5000
    assert len(cut["content"]) == 1000
    assert "max_chars" in cut["message"]
