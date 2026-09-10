"""Web interface tests: pages, API endpoints, and error statuses."""

import re

import pytest
from fastapi.testclient import TestClient

from flanner.web import WEB_DIR, app, markdown_filter

# The Host header the middleware expects. TestClient defaults to
# "testserver", which flanner refuses on purpose: a Host it does not
# serve is how DNS rebinding reaches a local-only tool.
LOCAL_URL = "http://127.0.0.1:8080"

BAD_UUID = "not-a-uuid"
MISSING_UUID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def client(db):
    return TestClient(app, base_url=LOCAL_URL, follow_redirects=False)


@pytest.fixture
def project_id(client, git_repo):
    response = client.post(
        "/projects/new",
        data={"name": "webproj", "project_root": str(git_repo), "plan_directory": ".plans"},
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


@pytest.fixture
def plan_id(client, project_id):
    response = client.post(
        f"/projects/{project_id}/plans/new",
        data={"name": "webplan", "description": "d", "content": "# Web Plan v1\n"},
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def test_markdown_filter():
    assert markdown_filter(None) == ""
    assert markdown_filter("") == ""
    assert "<h1" in markdown_filter("# Title")


def test_markdown_filter_sanitizes_html():
    out = markdown_filter('# ok\n\n<script>alert(1)</script>\n\n<a href="javascript:x">j</a>')
    assert "<script" not in out
    assert "javascript:" not in out
    out2 = markdown_filter("<img src=x onerror=alert(1)>")
    assert "onerror" not in out2
    # legitimate formatting and code fences survive sanitization
    assert "<h1" in markdown_filter("# Title")
    assert "<pre" in markdown_filter("```python\nprint(1)\n```")
    assert "<table" in markdown_filter("| a | b |\n|---|---|\n| 1 | 2 |")


# --- project pages ---


def test_dashboard_with_activity(client, plan_id):
    response = client.get("/")
    assert response.status_code == 200
    assert "webplan" in response.text
    assert "Recent Activity" in response.text  # relabelled from "Recent Updates"


def test_create_project_invalid_git_root(client, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    response = client.post("/projects/new", data={"name": "x", "project_root": str(plain)})
    assert response.status_code == 200
    assert "not a valid git repository" in response.text


def test_create_project_no_root_no_git(client, outside_any_repo, monkeypatch):
    monkeypatch.chdir(outside_any_repo)
    response = client.post("/projects/new", data={"name": "x", "project_root": ""})
    assert response.status_code == 200
    assert "Could not find git repository" in response.text


def test_create_project_duplicate_name(client, project_id, git_repo):
    response = client.post(
        "/projects/new", data={"name": "webproj", "project_root": str(git_repo)}
    )
    assert response.status_code == 200
    assert "already exists" in response.text


def test_project_detail(client, project_id):
    response = client.get(f"/projects/{project_id}")
    assert response.status_code == 200
    assert "webproj" in response.text


def test_project_detail_errors(client):
    assert client.get(f"/projects/{BAD_UUID}").status_code == 400
    assert client.get(f"/projects/{MISSING_UUID}").status_code == 404


def test_delete_project_post(client, project_id):
    response = client.post(f"/projects/{project_id}/delete")
    assert response.status_code == 303
    assert client.get(f"/projects/{project_id}").status_code == 404


def test_delete_project_post_errors(client):
    assert client.post(f"/projects/{BAD_UUID}/delete").status_code == 400
    assert client.post(f"/projects/{MISSING_UUID}/delete").status_code == 404


# --- plan pages ---


def test_new_plan_form(client, project_id):
    assert client.get(f"/projects/{project_id}/plans/new").status_code == 200
    assert client.get(f"/projects/{BAD_UUID}/plans/new").status_code == 400
    assert client.get(f"/projects/{MISSING_UUID}/plans/new").status_code == 404


def test_create_plan_errors(client, project_id, plan_id):
    # Duplicate plan name: re-renders form with error
    response = client.post(
        f"/projects/{project_id}/plans/new", data={"name": "webplan", "content": "x"}
    )
    assert response.status_code == 200
    assert "already exists" in response.text

    assert (
        client.post(f"/projects/{BAD_UUID}/plans/new", data={"name": "p", "content": "c"})
    ).status_code == 400
    assert (
        client.post(f"/projects/{MISSING_UUID}/plans/new", data={"name": "p", "content": "c"})
    ).status_code == 404


def test_create_plan_no_project_root(client, db):
    from flanner.database import create_project, get_session

    rootless = create_project(get_session(), name="rootless-web")
    response = client.post(
        f"/projects/{rootless.id}/plans/new", data={"name": "p", "content": "c"}
    )
    assert response.status_code == 400


def test_plan_view_and_versions(client, plan_id):
    # Edit to create version 2
    response = client.post(
        f"/plans/{plan_id}/edit", data={"content": "# Web Plan v2\n", "notes": "n"}
    )
    assert response.status_code == 303

    latest = client.get(f"/plans/{plan_id}")
    assert latest.status_code == 200
    assert "Web Plan v2" in latest.text

    v1 = client.get(f"/plans/{plan_id}?version=1")
    assert v1.status_code == 200
    assert "Web Plan v1" in v1.text

    assert client.get(f"/plans/{plan_id}?version=99").status_code == 404


def test_plan_view_errors(client, plan_id):
    assert client.get(f"/plans/{BAD_UUID}").status_code == 400
    assert client.get(f"/plans/{MISSING_UUID}").status_code == 404


def test_plan_view_file_missing_on_disk(client, plan_id, git_repo):
    (git_repo / ".plans" / "webplan_v1.md").unlink()
    assert client.get(f"/plans/{plan_id}").status_code == 404


def test_plan_edit_page(client, plan_id):
    response = client.get(f"/plans/{plan_id}/edit")
    assert response.status_code == 200
    assert "Web Plan v1" in response.text

    assert client.get(f"/plans/{BAD_UUID}/edit").status_code == 400
    assert client.get(f"/plans/{MISSING_UUID}/edit").status_code == 404


def test_plan_edit_has_codemirror_over_textarea(client, plan_id):
    html = client.get(f"/plans/{plan_id}/edit").text
    # CodeMirror is loaded as a vendored asset...
    assert "vendor/codemirror/codemirror.min.js" in html
    assert "CodeMirror.fromTextArea" in html
    # ...but the plain textarea is still the form field (progressive enhancement).
    assert 'id="content"' in html and 'name="content"' in html


def test_codemirror_asset_is_served(client, plan_id):
    resp = client.get("/static/vendor/codemirror/codemirror.min.js")
    assert resp.status_code == 200
    assert "CodeMirror" in resp.text


def test_design_tokens_and_toast_shipped(client):
    tokens = client.get("/static/css/tokens.css").text
    assert "--r-md:" in tokens and "--shadow-pop:" in tokens  # radius + elevation tokens
    css = client.get("/static/css/shell.css").text
    assert ".toast-region" in css and ".toast--success" in css  # toast component
    js = client.get("/static/js/app.js").text
    assert "toast-region" in js and "aria-live" in js  # toast built with a live region


def test_theme_toggle_and_skip_link(client):
    html = client.get("/").text
    assert 'id="theme-toggle"' in html
    assert 'class="skip-link"' in html and 'href="#main"' in html
    assert 'aria-current="page"' in html  # active nav item marked
    css = client.get("/static/css/tokens.css").text
    assert ':root[data-theme="dark"]' in css  # manual dark overrides the OS setting


def test_command_palette_index_and_markup(client, plan_id, project_id):
    # the palette dialog and search trigger ship on every page
    html = client.get("/").text
    assert 'id="cmdk"' in html and 'id="cmdk-input"' in html
    assert 'id="cmdk-open"' in html  # discoverable search button in the nav
    # the search index lists both projects and their plans with jump URLs
    index = client.get("/api/search").json()
    proj = next(i for i in index if i["type"] == "project" and i["name"] == "webproj")
    assert proj["url"] == f"/projects/{project_id}"
    plan = next(i for i in index if i["type"] == "plan" and i["name"] == "webplan")
    assert plan["url"] == f"/plans/{plan_id}" and plan["context"] == "webproj"


def test_list_sort_filter_controls(client, project_id, plan_id):
    # projects list: filter input + sort select over sortable rows
    projects = client.get("/projects").text
    assert "data-listgroup" in projects and "data-list-filter" in projects
    assert 'data-name="webproj"' in projects and "data-files=" in projects
    # project detail plan list gets the same controls, with an updated-at key
    detail = client.get(f"/projects/{project_id}").text
    assert "data-list-sort" in detail and 'data-name="webplan"' in detail
    assert "data-updated=" in detail


def test_projects_sort_control_actually_sorts(client, git_repo, tmp_path):
    """The control shipped for months without the route reading the parameter.

    It rendered, it round-tripped, and it changed nothing: `sort` was never a
    parameter of the view, so `?sort=name` was silently discarded. Asserting on
    the order rather than on the markup is the only version of this test that
    would have failed.
    """
    from flanner.server import create_project_tool

    for name in ("zulu-service", "alpha-service", "mike-service"):
        root = tmp_path / name
        root.mkdir()
        (root / ".git").mkdir()
        create_project_tool(name=name, project_root=str(root), plan_directory=".plans")

    def names(query: str) -> list[str]:
        body = client.get(f"/projects{query}").text
        return re.findall(r'data-name="([^"]+)"', body)

    by_name = names("?sort=name")
    assert by_name == sorted(by_name), by_name
    # The default is not alphabetical, so the two orders must differ.
    assert names("") != by_name
    # A nonsense value falls back instead of erroring.
    assert client.get("/projects?sort=nonsense").status_code == 200


def test_download_serves_the_file_rather_than_a_disk_path(client, plan_id):
    """The button pointed at the absolute path stored in the database.

    That is not a URL, so every click asked this server for a path starting
    with a drive letter and got a 404. Asserting on the response rather than
    on the presence of a button is the only version that would have caught it.
    """
    page = client.get(f"/plans/{plan_id}").text
    href = re.search(r'href="([^"]+)"[^>]*download', page).group(1)
    assert href.startswith("/plans/"), href

    got = client.get(href)
    assert got.status_code == 200
    assert "# Web Plan v1" in got.text
    assert "attachment" in got.headers["content-disposition"]
    assert "webplan_v1.md" in got.headers["content-disposition"]


def test_download_refuses_anything_but_a_known_version(client, plan_id):
    assert client.get(f"/plans/{BAD_UUID}/download").status_code == 400
    assert client.get(f"/plans/{MISSING_UUID}/download").status_code == 404
    assert client.get(f"/plans/{plan_id}/download?version=99").status_code == 404


def test_tier3_craft_signals(client, plan_id):
    # SVG favicon is served and referenced, with theme-color meta for both schemes
    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200 and "<svg" in favicon.text
    home = client.get("/")
    assert 'rel="icon"' in home.text and "favicon.svg" in home.text
    assert 'name="theme-color"' in home.text and "prefers-color-scheme: dark" in home.text
    # dashboard shows a real "updated this week" count, not the capped-list length
    assert "Updated this week" in home.text
    css = client.get("/static/css/shell.css").text
    assert "@media print" in css  # print a plan as a document
    assert "tabular-nums" in css  # aligned numeric figures
    assert "::selection" in css and "scrollbar-color" in css


def test_tier2_polish_shipped(client, project_id):
    css = client.get("/static/css/shell.css").text
    assert "@view-transition" in css  # smooth cross-page transitions
    js = client.get("/static/js/app.js").text
    assert "rel = 'prefetch'" in js or "'prefetch'" in js  # hover prefetch
    # keyboard-shortcuts help sheet ships on every page
    home = client.get("/").text
    assert 'id="help"' in home and "Keyboard shortcuts" in home
    # inline duplicate-name validation on the new-project and new-plan forms
    newproj = client.get("/projects/new").text
    assert 'data-check-unique="project"' in newproj and 'class="field-error"' in newproj
    newplan = client.get(f"/projects/{project_id}/plans/new").text
    assert 'data-check-unique="plan"' in newplan and "data-check-scope=" in newplan


def test_plan_view_has_reading_settings(client, plan_id):
    html = client.get(f"/plans/{plan_id}").text
    assert 'id="reading-panel"' in html
    assert 'data-reading="preset"' in html
    assert 'data-reading="font"' in html
    # a11y: segmented groups are labelled, and the version select has a real label
    assert 'aria-labelledby="rl-preset"' in html
    assert 'for="version-selector"' in html


def test_plan_update_no_changes(client, plan_id):
    response = client.post(f"/plans/{plan_id}/edit", data={"content": "# Web Plan v1\n"})
    assert response.status_code == 303
    assert "no_changes" in response.headers["location"]


def test_resaving_untouched_content_makes_no_new_version(client, plan_id):
    """A browser submits a textarea as CRLF, whatever the platform.

    So content that came back from the editor untouched is not byte-identical
    to the content that went in, and the change check compared raw bytes. The
    result was a new, identical version on every save through the web editor.
    The comparison is over the normalised form now.
    """
    before = client.get(f"/plans/{plan_id}").text.count("vtag")

    crlf = "# Web Plan v1\r\n"
    response = client.post(f"/plans/{plan_id}/edit", data={"content": crlf})
    assert response.status_code == 303
    assert "no_changes" in response.headers["location"], response.headers["location"]

    # And the reader is told why nothing happened.
    landed = client.get(f"/plans/{plan_id}?message=no_changes").text
    assert "No changes detected" in landed
    assert client.get(f"/plans/{plan_id}").text.count("vtag") == before


def test_a_real_edit_still_makes_a_version(client, plan_id):
    """The guard must not swallow genuine edits."""
    response = client.post(
        f"/plans/{plan_id}/edit",
        data={"content": "# Web Plan v1\r\nplus a line\r\n"},
    )
    assert response.status_code == 303
    assert "no_changes" not in response.headers["location"]
    assert "plus a line" in client.get(f"/plans/{plan_id}").text


def test_plan_update_errors(client):
    assert client.post(f"/plans/{BAD_UUID}/edit", data={"content": "c"}).status_code == 400
    assert client.post(f"/plans/{MISSING_UUID}/edit", data={"content": "c"}).status_code == 404


def test_plan_history(client, plan_id):
    response = client.get(f"/plans/{plan_id}/history")
    assert response.status_code == 200

    assert client.get(f"/plans/{BAD_UUID}/history").status_code == 400
    assert client.get(f"/plans/{MISSING_UUID}/history").status_code == 404


# --- API endpoints ---


def test_api_projects_and_plans(client, project_id, plan_id):
    projects = client.get("/api/projects").json()
    assert any(p["id"] == project_id for p in projects)

    plans = client.get(f"/api/projects/{project_id}/plans").json()
    assert plans[0]["name"] == "webplan"

    assert client.get(f"/api/projects/{BAD_UUID}/plans").status_code == 400


def test_api_get_plan(client, plan_id):
    data = client.get(f"/api/plans/{plan_id}").json()
    assert data["plan_file"]["name"] == "webplan"
    assert "# Web Plan v1" in data["content"]
    assert data["frontmatter"]["mcp_plan_file"] is True

    versioned = client.get(f"/api/plans/{plan_id}?version=1").json()
    assert versioned["version"]["version"] == 1


def test_api_get_plan_errors(client, plan_id, git_repo):
    assert client.get(f"/api/plans/{BAD_UUID}").status_code == 400
    assert client.get(f"/api/plans/{MISSING_UUID}").status_code == 404
    assert client.get(f"/api/plans/{plan_id}?version=99").status_code == 404

    (git_repo / ".plans" / "webplan_v1.md").unlink()
    assert client.get(f"/api/plans/{plan_id}").status_code == 404


def test_api_delete_project(client, project_id):
    result = client.delete(f"/api/projects/{project_id}").json()
    assert result["success"] is True

    assert client.delete(f"/api/projects/{BAD_UUID}").status_code == 400
    assert client.delete(f"/api/projects/{MISSING_UUID}").status_code == 404


# --- linear surfacing in the web UI ---


def test_plan_view_shows_linear_panel(client, plan_id, project_id):
    from uuid import UUID

    from flanner.database import create_linear_config, create_linear_link, get_session

    session = get_session()
    create_linear_config(session, UUID(project_id), "acme")
    create_linear_link(
        session, UUID(plan_id), "ENG-42", issue_title="Do it", issue_state="In Progress"
    )

    html = client.get(f"/plans/{plan_id}").text
    assert 'class="linear-panel"' in html
    assert "ENG-42" in html
    assert "https://linear.app/acme/issue/ENG-42" in html
    assert "In Progress" in html


def test_plan_view_no_panel_when_unlinked(client, plan_id):
    assert 'class="linear-panel"' not in client.get(f"/plans/{plan_id}").text


def test_project_detail_linear_marker(client, plan_id, project_id):
    from uuid import UUID

    from flanner.database import create_linear_link, get_session

    create_linear_link(get_session(), UUID(plan_id), "ENG-7")
    assert "linear-marker" in client.get(f"/projects/{project_id}").text


def _import_outside_review(plan_id, reviewer="Dana at Acme", version=None):
    """Put an outside review against a plan, the way the CLI would."""
    from uuid import UUID

    from flanner.database import get_plan_file, get_project, get_session
    from flanner.review import import_external

    session = get_session()
    plan_file = get_plan_file(session, UUID(plan_id))
    project = get_project(session, plan_file.project_id)
    return import_external(
        session,
        project=project,
        plan_file=plan_file,
        reviewer=reviewer,
        notes=[{"quote": "Web Plan", "body": "Who owns this?", "occurrence": 0}],
        reviewed_version=version,
    )


def test_the_plan_page_shows_notes_from_outside(client, plan_id):
    _import_outside_review(plan_id)
    body = client.get(f"/plans/{plan_id}").text
    assert "From outside" in body
    assert "Who owns this?" in body
    assert "Dana at Acme" in body


def test_outside_notes_are_labelled_unverified(client, plan_id):
    """The reviewer had no device key. A page that showed their note the
    same way as a teammate's would be the one unforgivable bug here."""
    _import_outside_review(plan_id)
    body = client.get(f"/plans/{plan_id}").text
    assert "unverified" in body
    assert "received rather than authored" in body


def test_a_plan_with_no_outside_review_shows_no_such_panel(client, plan_id):
    assert "From outside" not in client.get(f"/plans/{plan_id}").text


def test_outside_notes_are_anchored_to_the_version_reviewed(client, plan_id):
    """Recording them against a later revision the reviewer never saw would
    misattribute every one of them."""
    _import_outside_review(plan_id, version=1)
    assert "on v1" in client.get(f"/plans/{plan_id}").text


def test_the_review_page_counts_outside_notes(client, plan_id):
    _import_outside_review(plan_id)
    body = client.get("/review").text
    assert "outside review" in body


def test_the_review_page_says_when_a_decision_would_bind_nobody(client, plan_id):
    """A solo project projects review against a role map anyone can edit.

    The page draws the same states either way, so without this a reader
    cannot tell a rehearsal from an authorization. The reason travels too:
    "advisory" without a why is just a word.
    """
    _import_outside_review(plan_id)

    body = client.get("/review").text

    # The pill, not the word: the footnote below the table explains what
    # "advisory" means and would satisfy a bare substring check even with an
    # empty table.
    assert ">advisory</span>" in body
    assert "has not joined a workspace" in body
    assert "+1" in body


def test_a_plan_with_only_outside_review_still_appears(client, plan_id):
    """It has no proposal, so the old filter dropped it entirely - which is
    exactly the plan somebody is waiting to hear about."""
    before = client.get("/review").text
    assert "Nothing is waiting" in before
    _import_outside_review(plan_id)
    after = client.get("/review").text
    assert "Nothing is waiting" not in after


# --- requests from another website ------------------------------------------
#
# These are regression tests for a hole that was open and exploitable: a plain
# form POST from any page in the user's browser deleted projects here, because
# the UI binds localhost and trusted that entirely.


def test_a_form_post_from_another_site_is_refused(client, project_id):
    """The bug. A page on any site could submit this and it worked."""
    response = client.post(
        f"/projects/{project_id}/delete",
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "evil.example" in response.text
    # And the project is still here.
    assert client.get(f"/projects/{project_id}").status_code == 200


def test_our_own_forms_still_work(client, project_id):
    """A same-origin post carries an Origin too, and must not be caught."""
    response = client.post(
        f"/projects/{project_id}/delete",
        headers={"Origin": LOCAL_URL},
    )
    assert response.status_code == 303


def test_a_request_with_no_origin_is_allowed(client, git_repo):
    """curl, and the MCP server calling /ipc/call. Neither carries cookies."""
    response = client.post(
        "/projects/new",
        data={"name": "noorigin", "project_root": str(git_repo), "plan_directory": ".plans"},
    )
    assert response.status_code == 303


def test_a_foreign_host_header_is_refused(client):
    """DNS rebinding: an attacker domain resolved to 127.0.0.1 is same-origin.

    Refusing on Host is what stops them reading the catalog to find the ids
    the delete route needs.
    """
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_a_cross_site_read_is_still_allowed(client):
    """GET is not state-changing, so Origin alone does not refuse it.

    The Host check is what protects reads; this pins that the Origin check
    is scoped to writes and does not quietly break embedding or link-outs.
    """
    assert client.get("/", headers={"Origin": "https://evil.example"}).status_code == 200


def test_an_operator_can_serve_elsewhere(client, monkeypatch):
    """`flanner web --host` sets this, and the Host check stands down."""
    monkeypatch.setenv("FLANNER_WEB_HOSTS", "*")
    assert client.get("/", headers={"Host": "flanner.internal"}).status_code == 200


# --- sharing a memory from the page ------------------------------------------


@pytest.fixture
def a_memory(client, project_id):
    from flanner.services import dispatch

    result = dispatch(
        "memory_remember",
        {
            "content": "Use advisory locks; the tool must work offline.",
            "category": "decision",
            "project_id": project_id,
        },
    )
    return result["id"]


@pytest.fixture
def a_personal_memory(client, project_id):
    from flanner.services import dispatch

    result = dispatch(
        "memory_remember",
        {
            "content": "Prefers blockers over nitpicks.",
            "category": "preference",
            "scope": "personal",
        },
    )
    return result["id"]


def test_a_project_memory_offers_a_share_button(client, a_memory):
    page = client.get(f"/memory/{a_memory}").text

    assert "Share with the team" in page
    assert "joining a workspace shares nothing by itself" in page


def test_personal_memory_offers_no_button_at_all(client, a_personal_memory):
    """The refusal lives in the domain. Offering a control that always fails
    would teach somebody the rule by wasting their time."""
    page = client.get(f"/memory/{a_personal_memory}").text

    assert "Share with the team" not in page
    assert "cannot be shared with anybody" in page


def test_sharing_without_a_workspace_says_so_rather_than_failing_silently(client, a_memory):
    response = client.post(f"/memory/{a_memory}/share")

    assert response.status_code == 303
    assert "has%20not%20joined%20a%20workspace" in response.headers["location"]


# --- paging -------------------------------------------------------------------


def _rows(html: str) -> int:
    return html.count("data-list-item")


def test_plans_page_pages_fifteen_at_a_time_and_remembers_the_size(client, project_id):
    for n in range(17):
        made = client.post(
            f"/projects/{project_id}/plans/new",
            data={"name": f"plan{n:02d}", "description": "d", "content": "# P\n"},
        )
        assert made.status_code == 303

    first = client.get("/plans")
    assert first.status_code == 200
    assert _rows(first.text) == 15
    assert "1–15 of 17" in first.text

    second = client.get("/plans?page=2")
    assert _rows(second.text) == 2
    assert "16–17 of 17" in second.text
    # Past the end lands on the last page; junk falls back rather than erroring.
    assert _rows(client.get("/plans?page=99").text) == 2
    assert client.get("/plans?page=x&per=999").status_code == 200

    wide = client.get("/plans?per=30")
    assert _rows(wide.text) == 17
    assert wide.cookies.get("flanner_per_page") == "30"

    # The choice sticks without the parameter, and covers every list.
    client.cookies.set("flanner_per_page", "30")
    assert _rows(client.get("/plans").text) == 17
    project = client.get(f"/projects/{project_id}")
    assert project.status_code == 200 and "1–17 of 17" in project.text


# --- one skill's page ---------------------------------------------------------


@pytest.fixture
def a_skill(client, git_repo, monkeypatch):
    """A skill in the repository the server is looking at.

    The route reads the working directory, so the test moves into the
    fixture's repository rather than the checkout the suite is running in.
    """
    monkeypatch.chdir(git_repo)
    for agent, folder in (("claude-code", ".claude"), ("codex", ".agents")):
        package = git_repo / folder / "skills" / "deploy"
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(
            f"---\nname: deploy\ndescription: Ship it, for {agent}\n---\n\n## How\n\nRun it.\n",
            encoding="utf-8",
        )
    return git_repo


def test_a_skill_has_a_page_of_its_own(client, a_skill):
    page = client.get("/skills/deploy")
    assert page.status_code == 200
    # Both agents' copies, each named, and each loading its own.
    assert page.text.count("cols-copies") >= 3  # the head and two rows
    assert "claude-code" in page.text and "codex" in page.text
    assert "each load their own" in page.text
    # What it says, rendered from the manifest rather than the frontmatter.
    assert "Run it." in page.text


def test_the_index_links_to_it(client, a_skill):
    assert '/skills/deploy"' in client.get("/skills").text


def test_a_name_nobody_has_is_not_a_page(client, a_skill):
    assert client.get("/skills/no-such-skill").status_code == 404


def test_proposals_is_a_page_rather_than_a_skill(client, a_skill):
    """`/skills/{name}` is declared last, so the literal paths still win."""
    assert client.get("/skills/proposals").status_code == 200


def test_keeping_a_copy_stores_it_and_says_so(client, a_skill):
    kept = client.post("/skills/deploy/adopt", data={"agent": "claude-code"})
    assert kept.status_code == 303
    assert "kept" in kept.headers["location"]

    page = client.get("/skills/deploy")
    assert "verifies" in page.text  # the snapshot is in the store and checks out
    # And now it can be sent, once there is somewhere to send it.
    assert "has not joined a workspace" in page.text


def test_sharing_without_a_workspace_refuses_rather_than_pretending(client, a_skill):
    sent = client.post(
        "/skills/deploy/share",
        data={"manifest_hash": "sha256:whatever", "agent": "claude-code"},
    )
    assert sent.status_code == 303
    assert "has+not+joined+a+workspace" in sent.headers["location"]


def test_a_form_cannot_send_somebody_off_this_area(client, a_skill):
    """`back` goes straight into a Location header, so it is checked."""
    away = client.post(
        "/skills/channel",
        data={"name": "deploy", "action": "subscribe", "back": "//evil.example/"},
    )
    assert away.headers["location"].startswith("/skills?")


def test_the_index_shows_both_agents_and_filters_to_one(client, a_skill):
    """The index scans every agent; the filters narrow what is shown.

    Narrowing the scan instead would file half a machine's skills in the
    catalog and compute collisions against the other half.
    """
    every = client.get("/skills?shadowed=1")
    assert every.status_code == 200
    assert "claude-code" in every.text and "codex" in every.text
    assert every.text.count("data-list-item") == 2

    only = client.get("/skills?agent=codex&shadowed=1")
    assert only.text.count("data-list-item") == 1
    assert ".agents" in only.text and ".claude" not in only.text.split("data-list")[1]

    # The tiles say how the machine splits, and the menu offers what it has.
    assert 'value="codex"' in every.text and 'id="skagent"' in every.text


def test_every_grid_head_matches_the_columns_its_css_defines():
    """A row with more spans than the grid has columns wraps the last one
    onto a line of its own. It looks like a styling accident rather than a
    bug, which is how one shipped: the Agent column was added to the skills
    table and `.cols-skills` still declared five.
    """
    import re

    css = (WEB_DIR / "static/css/shell.css").read_text(encoding="utf-8")
    # Comments go first: a comma inside one splits into the selector list
    # and hides the rule behind it — which is how the first draft of this
    # test passed while `.cols-skills` went unchecked.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # Base rules only. A narrow screen collapses every one of these to a
    # single column on purpose, and that override is not the shape the head
    # row has to match.
    css = re.sub(r"@media[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", "", css)
    declared = {}
    for rule in re.finditer(r"([^{}]+)\{\s*grid-template-columns:\s*([^;}]+)", css):
        for selector in rule.group(1).split(","):
            selector = selector.strip()
            if selector.startswith(".cols-"):
                declared[selector[1:]] = len(rule.group(2).split())

    checked = 0
    seen = set()
    for template in (WEB_DIR / "templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        for head in re.finditer(
            r'class="[^"]*grid-head[^"]*\b(cols-[\w-]+)[^"]*">(.*?)</div>', text, re.S
        ):
            name, body = head.group(1), head.group(2)
            if name not in declared:
                continue
            spans = body.count("<span")
            assert (
                spans == declared[name]
            ), f"{template.name}: .{name} has {spans} spans and {declared[name]} columns"
            checked += 1
            seen.add(name)
    assert checked >= 18, f"only {checked} grid heads found; the pattern must have changed"
    assert "cols-skills" in seen, "the skills table is the one that shipped this bug"


def test_the_skills_index_sections_are_tabs_that_survive_no_javascript(client, a_skill):
    """Every panel is rendered; the script hides the ones you are not reading.

    Built as anchors for that reason. If the tabs were the only way to
    reach a section, a page without JavaScript would lose five sixths of
    itself — including the table it exists for.
    """
    page = client.get("/skills").text
    for section in ("tab-skills", "tab-usage", "tab-versions", "tab-team", "tab-health"):
        assert f'id="{section}"' in page, section
        assert f'href="#{section}"' in page, section
    # Nothing is hidden server-side, so the markup alone is complete.
    assert "data-tab-panel hidden" not in page and 'data-tab-panel="hidden"' not in page
    # The defect banner points at the tab that holds the list, not at "below".
    assert "at the foot of this page" not in page


def test_the_roots_list_is_closed_but_still_answers(client, a_skill):
    """A couple of dozen directories somebody consults when a skill is
    missing, so it starts closed — and the line you see while it is closed
    carries the answer it usually gives.

    A native `<details>`, so it opens with no script and takes the
    keyboard for free.
    """
    page = client.get("/skills").text
    assert '<details class="card card-pad disclose"' in page
    assert "<summary>Where this was read from" in page
    assert " open>" not in page.split("Where this was read from")[0][-200:]
    # Codex looks in three places and two of them are absent on a fresh
    # repository, so the summary has a number to report either way.
    assert "directories" in page and "not there" in page
