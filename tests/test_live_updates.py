"""Telling the page that a peer changed something.

The catalog has several writers and they are separate processes: this web
UI, `flanner peer serve` taking a push from a teammate, the MCP server
acting for an agent, a `flanner sync` in a terminal. None of them are in
this process's call stack, so nothing here can be notified in-process.

So the server asks the catalog what moved rather than waiting to be told.
The tests below write the way each of those writers does — straight to the
store, with no notification — and then check the stream noticed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603,S607


@pytest.fixture
def short_stream(monkeypatch):
    """A stream that gives up in a moment rather than in five minutes.

    The endpoint ends its own connections on purpose; these tests only
    shorten that so a run does not wait out the real one.
    """
    from flanner import web

    monkeypatch.setattr(web, "LIVE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(web, "LIVE_STREAM_MAX_SECONDS", 0.5)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo.parent, "init", "-q", str(repo))

    from flanner.database import create_project, get_session, init_database

    init_database(str(tmp_path / "home" / "data.db"))
    session = get_session()
    project = create_project(session, name="p", project_root=str(repo), auto_gitignore=False)
    session.commit()

    from flanner.web import app

    return TestClient(app, base_url="http://127.0.0.1"), session, project


def _snapshot(session):
    from flanner.web import _catalog_snapshot

    return _catalog_snapshot(session)


# --- what counts as a change ------------------------------------------------


def test_a_new_plan_shows_up_in_the_snapshot(store):
    client, session, project = store
    from flanner.plan_ops import create_plan

    before = _snapshot(session)
    plan_file, _ = create_plan(
        session, project=project, name="arch", content="# one\n", created_by="me"
    )
    session.commit()

    after = _snapshot(session)
    assert set(after) - set(before) == {str(plan_file.id)}


def test_a_version_that_does_not_move_the_pointer_is_still_a_change(store):
    """The case the obvious marker misses.

    A version arriving from a peer deliberately does not move the plan's
    current-version pointer — that is the rule that stops a teammate
    changing what you have open. So watching `current_version`, or
    `updated_at`, would see nothing at all for exactly the event this
    feature exists to report. The signature counts versions too.
    """
    client, session, project = store
    from flanner.database import create_version, get_plan_file
    from flanner.plan_ops import create_plan

    plan_file, _ = create_plan(
        session, project=project, name="arch", content="# one\n", created_by="me"
    )
    session.commit()
    before = _snapshot(session)
    pointer_before = get_plan_file(session, plan_file.id).current_version

    # Exactly what materialising a peer's version does: a new version row,
    # and the pointer left alone.
    create_version(
        session,
        plan_file_id=plan_file.id,
        version=99,
        file_path=str(Path(project.project_root) / "arch_v99.md"),
        content_hash="deadbeef",
        created_by="alice",
        notes="",
        artifact_id="sha256:whatever",
    )
    session.commit()

    after = _snapshot(session)
    assert get_plan_file(session, plan_file.id).current_version == pointer_before
    assert after[str(plan_file.id)] != before[str(plan_file.id)], (
        "a peer's version arrived and the catalog signature did not move"
    )


def test_accepting_a_baseline_is_a_change(store):
    client, session, project = store
    from flanner.database import get_plan_file
    from flanner.plan_ops import create_plan

    plan_file, _ = create_plan(
        session, project=project, name="arch", content="# one\n", created_by="me"
    )
    session.commit()
    before = _snapshot(session)

    get_plan_file(session, plan_file.id).current_version = 7
    session.commit()

    assert _snapshot(session)[str(plan_file.id)] != before[str(plan_file.id)]


def test_gaining_a_peer_is_a_change_though_no_plan_moved(store):
    """The mesh page reads the peer keyring, and the keyring is not in the
    database — it is in the cached session, written by `flanner login` and by
    a key exchange. Watching only plans would leave that page stale."""
    client, session, project = store
    from flanner import session as session_cache

    before = _snapshot(session)
    path = session_cache.session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"device_keys": {"abc": "key"}}', encoding="utf-8")

    assert _snapshot(session)["mesh"] != before["mesh"]


def test_the_mesh_entry_is_not_mistaken_for_a_plan(store):
    """It shares the dict with plan ids, so it must not look like one."""
    client, session, project = store

    assert "mesh" in _snapshot(session)
    with pytest.raises(ValueError):
        UUID("mesh")


def test_reading_the_catalog_twice_reports_nothing(store):
    """A poll that always looks changed would refresh the page every second."""
    client, session, project = store
    from flanner.plan_ops import create_plan

    create_plan(session, project=project, name="arch", content="# one\n", created_by="me")
    session.commit()

    assert _snapshot(session) == _snapshot(session)


# --- the stream -------------------------------------------------------------


def test_the_stream_opens_and_says_so_before_anything_happens(store, short_stream):
    """A silent socket and a broken one look identical, and a browser that
    just reconnected deserves to know which it has."""
    client, _, _ = store
    with client.stream("GET", "/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("event:"):
                assert line.strip() == "event: ready"
                break


# --- what a page does with it -----------------------------------------------


def test_a_plan_page_says_which_plan_and_version_it_is_showing(store):
    """The banner compares against this. Without it the page cannot tell a
    version it is already displaying from one that just arrived."""
    client, session, project = store
    from flanner.plan_ops import create_plan

    plan_file, _ = create_plan(
        session, project=project, name="arch", content="# one\n", created_by="me"
    )
    session.commit()

    page = client.get(f"/plans/{plan_file.id}")
    assert f'data-live-plan="{plan_file.id}"' in page.text
    assert 'data-live-version="1"' in page.text


def test_the_revision_endpoint_answers_what_a_page_needs(store):
    client, session, project = store
    from flanner.plan_ops import create_plan

    plan_file, _ = create_plan(
        session, project=project, name="arch", content="# one\n", created_by="me"
    )
    session.commit()

    body = client.get(f"/plans/{plan_file.id}/revision").json()
    assert body == {"version": 1, "name": "arch"}


def test_a_revision_for_a_plan_that_does_not_exist_is_a_404(store):
    client, _, _ = store
    assert client.get("/plans/not-a-uuid/revision").status_code == 404
    assert client.get("/plans/00000000-0000-0000-0000-000000000000/revision").status_code == 404


def test_a_plans_history_watches_that_plan_and_nothing_else(store):
    """Every other plan in the store moving is not news on this page, and
    re-rendering it each time one did would be a page that will not sit
    still."""
    client, session, project = store
    from flanner.plan_ops import create_plan

    plan_file, _ = create_plan(
        session, project=project, name="arch", content="# one\n", created_by="me"
    )
    session.commit()

    page = client.get(f"/plans/{plan_file.id}/history")
    assert f'data-live-list="{plan_file.id}"' in page.text


def test_the_pages_that_show_what_arrived_are_all_marked(store):
    """Every page whose content a peer can change without you touching it."""
    client, session, project = store
    from flanner.plan_ops import create_plan

    create_plan(session, project=project, name="arch", content="# one\n", created_by="me")
    session.commit()

    for path in ("/", "/projects", "/plans", "/mesh"):
        assert "data-live-list" in client.get(path).text, path


def test_list_pages_are_marked_refreshable_and_the_editor_is_not(store):
    """The editor must never be swapped under somebody typing into it."""
    client, session, project = store
    from flanner.plan_ops import create_plan

    plan_file, _ = create_plan(
        session, project=project, name="arch", content="# one\n", created_by="me"
    )
    session.commit()

    assert "data-live-list" in client.get("/").text
    assert "data-live-list" in client.get("/projects").text
    editor = client.get(f"/plans/{plan_file.id}/edit")
    assert "data-live-list" not in editor.text, "the editor would re-render while you type"


def test_the_stream_is_not_cached_anywhere(store, short_stream):
    client, _, _ = store
    with client.stream("GET", "/events") as response:
        assert response.headers.get("cache-control") == "no-store"
        # nginx and friends buffer a streaming response into uselessness.
        assert response.headers.get("x-accel-buffering") == "no"


def test_the_stream_ends_itself_rather_than_polling_forever(store, short_stream):
    """A browser that vanishes without closing cleanly would otherwise leave
    this reading the database for the life of the process. Ending on purpose
    costs a reconnect the browser makes by itself."""
    client, _, _ = store

    body = client.get("/events").text

    assert body.startswith("event: ready"), body[:60]


def test_the_snapshot_is_two_queries_however_many_plans_there_are(store):
    """It runs once a second for the life of the process, so it must not be
    one query per plan."""
    client, session, project = store
    from flanner.plan_ops import create_plan

    for i in range(6):
        create_plan(session, project=project, name=f"p{i}", content="# x\n", created_by="me")
    session.commit()

    seen: list[str] = []
    from sqlalchemy import event as sa_event

    engine = session.get_bind()

    def record(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        _snapshot(session)
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)

    assert len(seen) == 2, f"{len(seen)} queries for 6 plans: {seen}"
