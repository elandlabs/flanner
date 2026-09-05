"""The drift table, delivered a row at a time.

Judging one plan means several git subprocesses. Judging all of them took
about nine seconds against a real store with a cold cache, and every one of
those seconds was a blank page: the answer was computed in full before the
first byte went out.

The work is unchanged. What changed is that the page arrives immediately and
each row follows as it is decided, so the first drifted plan is readable in
about a tenth of a second rather than after all eighteen are done.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flanner import freshness


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603,S607


@pytest.fixture
def drifted(tmp_path, monkeypatch):
    """A store holding one plan that cites code which has since been deleted."""
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _run(repo.parent, "init", "-q", str(repo))
    _run(repo, "config", "user.email", "t@test")
    _run(repo, "config", "user.name", "t")
    (repo / "src" / "legacy.py").write_text("def gone_function():\n    pass\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "add legacy")

    from flanner.database import create_project, get_session, init_database
    from flanner.plan_ops import create_plan

    init_database(str(tmp_path / "home" / "data.db"))
    session = get_session()
    project = create_project(session, name="p", project_root=str(repo), auto_gitignore=False)
    create_plan(
        session,
        project=project,
        name="architecture",
        content="# Arch\n\nSee `src/legacy.py` and `gone_function`.\n",
        created_by="me",
    )
    session.commit()

    # Now delete what the plan cites, which is what makes it drift.
    (repo / "src" / "legacy.py").unlink()
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "drop legacy")

    freshness.clear_cache()
    from flanner.web import app

    return TestClient(app, base_url="http://127.0.0.1")


def _messages(client) -> list[dict]:
    body = client.get("/freshness/stream").text
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def test_the_page_itself_does_no_git_work(drifted):
    """The shell is the point: it must not wait on a repository walk.

    Asserted as "the response mentions no plan", because the plan's name can
    only appear if the server judged it before answering.
    """
    freshness.clear_cache()
    page = drifted.get("/freshness")

    assert page.status_code == 200
    assert "architecture.md" not in page.text, "the page rendered rows before answering"
    assert "data-freshness-stream" in page.text, "nothing tells the script to fill it in"


def test_the_stream_says_how_much_there_is_before_judging_any_of_it(drifted):
    """The count arrives first, so the page can say "4 of 18" rather than
    spinning with no idea whether it is nearly done."""
    first = _messages(drifted)[0]

    assert first["total"] == 1


def test_a_drifted_plan_arrives_as_a_rendered_row(drifted):
    rows = [m for m in _messages(drifted) if m.get("html")]

    assert len(rows) == 1
    html = rows[0]["html"]
    assert "architecture.md" in html
    assert "data-list-item" in html, "the row cannot be filtered or sorted with the others"
    assert "data-drift=" in html, "nothing to insert it in severity order by"
    assert "gone_function" in html or "legacy.py" in html, "no evidence for the claim"


def test_the_last_line_carries_the_totals(drifted):
    last = _messages(drifted)[-1]

    assert last["done"] is True
    assert sum(last["tally"].values()) == 1
    assert last["tally"]["stale"] == 1, last["tally"]


def test_every_plan_is_counted_even_when_it_has_no_row(drifted):
    """Progress has to move for fresh plans too, or a store of mostly-fresh
    plans looks stuck at zero while it works."""
    messages = _messages(drifted)
    judged = sum(m.get("judged", 0) for m in messages)

    assert judged == messages[0]["total"]


def test_the_rows_are_the_same_markup_the_page_renders(drifted):
    """One partial, not two. A row built in the script would be a second
    copy of this markup, free to drift from the first."""
    streamed = next(m["html"] for m in _messages(drifted) if m.get("html"))
    rendered = drifted.get("/freshness?full=1").text

    for fragment in ('class="grid-row cols-freshness"', "architecture.md", 'class="pill pill-'):
        assert fragment in streamed, fragment
        assert fragment in rendered, fragment


def test_the_full_page_still_works_without_the_stream(drifted):
    """What the noscript link points at, and the fallback if fetch fails."""
    page = drifted.get("/freshness?full=1")

    assert page.status_code == 200
    assert "architecture.md" in page.text
    assert "data-freshness-stream" not in page.text, "it would try to stream over itself"


def test_the_second_visit_is_served_from_the_cache(drifted):
    """The stream is not a substitute for the cache; it is what makes the
    uncached case bearable."""
    freshness.clear_cache()
    _messages(drifted)
    calls: list[str] = []
    real = freshness._git

    def counted(root, *args):
        calls.append(args[0])
        return real(root, *args)

    freshness._git = counted
    try:
        _messages(drifted)
    finally:
        freshness._git = real

    assert "grep" not in calls, f"re-ran the expensive lookups: {calls}"
