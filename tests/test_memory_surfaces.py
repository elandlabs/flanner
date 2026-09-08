"""Memory where a person or an agent actually meets it.

The web pages, the doctor's findings, and the two things that decide
whether an agent uses memory at all: what the managed instruction block
says, and whether the write guard stops it filing a memory by hand.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flanner import agent_hooks, memory_ops
from flanner.database import create_project, get_session, init_database


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLANNER_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607

    init_database(str(home / "data.db"))
    session = get_session()
    project = create_project(session, name="demo", project_root=str(repo), auto_gitignore=False)
    session.commit()
    return session, project, repo


@pytest.fixture
def client(store):
    from flanner.web import app

    return TestClient(app, base_url="http://127.0.0.1")


# --- the web pages -----------------------------------------------------------


def test_the_memory_page_lists_what_is_remembered(client, store):
    session, project, repo = store
    memory, _ = memory_ops.remember(
        session,
        content="Use advisory locks rather than Redis.",
        category="decision",
        project=project,
    )

    page = client.get("/memory")

    assert page.status_code == 200
    assert "Use advisory locks rather than Redis." in page.text
    assert str(memory.id) in page.text


def test_an_empty_store_explains_what_memory_is_for(client, store):
    """The first thing anybody sees. A blank table teaches nothing."""
    page = client.get("/memory")

    assert page.status_code == 200
    assert "Nothing remembered yet" in page.text
    assert "flanner mem remember" in page.text


def test_the_page_searches(client, store):
    session, project, repo = store
    memory_ops.remember(
        session,
        content="Use advisory locks rather than Redis.",
        category="decision",
        project=project,
    )
    memory_ops.remember(
        session,
        content="The API allows twenty requests a second.",
        category="fact",
        project=project,
    )

    found = client.get("/memory?q=redis")

    assert "advisory locks" in found.text
    assert "twenty requests" not in found.text


def test_one_memory_shows_where_it_came_from(client, store):
    """Provenance beside the claim, so a reader can weigh it without
    going somewhere else to find out who said it."""
    session, project, repo = store
    memory, _ = memory_ops.remember(
        session,
        content="Use advisory locks rather than Redis.",
        category="decision",
        project=project,
        source_refs=["plan:architecture_v4"],
    )

    page = client.get(f"/memory/{memory.id}")

    assert page.status_code == 200
    assert "plan:architecture_v4" in page.text
    assert memory.file_path.replace("\\", "/").rsplit("/", 1)[-1] in page.text.replace("\\", "/")


def test_a_memory_that_does_not_exist_is_a_404(client, store):
    assert client.get("/memory/not-a-uuid").status_code == 404
    assert client.get("/memory/00000000-0000-0000-0000-000000000000").status_code == 404


def test_the_sidebar_carries_memory_on_every_page(client, store):
    """Unconditional, like Mesh and Review. An entry that appears only when
    a count is computed vanishes from every page that forgets to."""
    session, project, repo = store
    memory_ops.remember(session, content="A decision.", category="decision", project=project)

    for path in ("/", "/projects", "/plans", "/memory"):
        assert 'href="/memory"' in client.get(path).text, path


# --- doctor ------------------------------------------------------------------


def test_doctor_reports_a_memory_whose_file_is_gone(store):
    from flanner.cli import _memory_findings

    session, project, repo = store
    memory, _ = memory_ops.remember(
        session, content="A decision.", category="decision", project=project
    )
    Path(memory.file_path).unlink()

    kinds = [f.kind for f in _memory_findings(session)]

    assert "mem_missing_file" in kinds


def test_doctor_reports_a_memory_edited_by_hand(store):
    """Editing a memory file is allowed. Serving the old text afterwards
    is not, so the disagreement has to surface."""
    from flanner.cli import _memory_findings

    session, project, repo = store
    memory, _ = memory_ops.remember(
        session, content="A decision.", category="decision", project=project
    )
    path = Path(memory.file_path)
    path.write_text(path.read_text(encoding="utf-8") + "\nAnd more.\n", encoding="utf-8")

    findings = _memory_findings(session)

    assert [f.kind for f in findings] == ["mem_hash_mismatch"]
    assert findings[0].plan.startswith("A decision")


def test_doctor_says_nothing_when_memory_agrees(store):
    from flanner.cli import _memory_findings

    session, project, repo = store
    memory_ops.remember(session, content="A decision.", category="decision", project=project)

    assert _memory_findings(session) == []


def test_doctor_reports_a_python_without_the_search_index(store, monkeypatch):
    """A slower search is worth saying out loud; discovering it as a
    mysterious ranking failure is not."""
    from flanner import database as db
    from flanner.cli import _memory_findings

    session, project, repo = store
    monkeypatch.setattr(db, "SEARCH_INDEX_AVAILABLE", False)

    kinds = [f.kind for f in _memory_findings(session)]

    assert "search_index_unavailable" in kinds


# --- what the agent is told --------------------------------------------------


def test_the_managed_block_tells_an_agent_to_recall_first(store):
    """A session that never recalls has nothing to show for every memory it
    saved, so this is the instruction that decides whether memory pays."""
    session, project, repo = store

    block = agent_hooks.agent_md_block(project)

    assert "memory_recall" in block
    assert "At the start of a task" in block


def test_the_managed_block_names_memory_as_data(store):
    """The whole prompt-injection surface, stated where the agent reads it
    rather than only in the tool response."""
    session, project, repo = store

    block = agent_hooks.agent_md_block(project)

    assert "not instructions" in block


def test_the_managed_block_still_covers_plans(store):
    """Memory is an addition, not a replacement."""
    session, project, repo = store

    block = agent_hooks.agent_md_block(project)

    assert "create_plan_file_tool" in block
    assert "## Plan files (managed by flanner)" in block
    assert "## Memory (managed by flanner)" in block


# --- the write guard ---------------------------------------------------------


def _write(session, target: Path, cwd: Path):
    return agent_hooks.decide_write(
        {"tool_input": {"file_path": str(target)}, "cwd": str(cwd)}, session
    )


def test_writing_a_memory_file_by_hand_is_refused(store):
    """Written by hand it has no id, no hash and no row, so nothing would
    ever recall it. It would look like it worked."""
    session, project, repo = store

    decision = _write(session, repo / ".flanner" / "memory" / "mine.md", repo)

    assert decision is not None
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert "memory_remember" in reason
    assert "invisible" in reason


def test_the_plan_guard_still_works(store):
    session, project, repo = store

    decision = _write(session, repo / ".plans" / "architecture_v1.md", repo)

    assert decision is not None
    assert "create_plan_file_tool" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_the_policy_file_is_not_guarded(store):
    """It is configuration a person is meant to edit, not a record flanner
    owns the shape of."""
    session, project, repo = store

    assert _write(session, repo / ".flanner" / "memory-policy.yml", repo) is None


def test_ordinary_files_are_left_alone(store):
    session, project, repo = store

    for target in (repo / "README.md", repo / "src" / "main.py", repo / ".flanner" / "notes.txt"):
        assert _write(session, target, repo) is None, target


# --- the pending queue --------------------------------------------------------


def _propose(session, project, **kwargs):
    from flanner.memory_policy import Policy

    return memory_ops.consider(
        session,
        [memory_ops.Candidate(**kwargs)],
        policy=Policy(),
        project=project,
    )[0]


def test_the_pending_page_shows_what_is_waiting(client, store):
    session, project, repo = store
    _propose(
        session,
        project,
        content="Use advisory locks rather than Redis.",
        category="decision",
        why_durable="settles how the queue works",
    )

    page = client.get("/memory/pending")

    assert page.status_code == 200
    assert "advisory locks" in page.text
    assert "settles how the queue works" in page.text


def test_the_pending_page_says_nothing_is_recalled_yet(client, store):
    """The distinction the whole mode rests on. A page that showed
    suggestions as memories would make approving meaningless."""
    session, project, repo = store
    _propose(session, project, content="A decision.", category="decision")

    page = client.get("/memory/pending")

    assert "Suggestions, not memories" in page.text
    assert "None of these is being recalled" in page.text


def test_pending_is_read_as_a_page_not_an_id(client, store):
    """`/memory/pending` and `/memory/{id}` share a shape, so the order
    they are declared in decides which one answers."""
    assert client.get("/memory/pending").status_code == 200


def test_an_empty_queue_explains_why_it_is_empty(client, store):
    """Empty because nothing was offered and empty because capture is off
    are different situations with the same appearance."""
    page = client.get("/memory/pending")

    assert page.status_code == 200
    assert "Nothing waiting" in page.text
    assert "suggest" in page.text


def test_the_badge_counts_what_needs_a_decision(client, store):
    """A number nobody has to act on teaches people to stop reading it."""
    session, project, repo = store
    memory_ops.remember(session, content="Already kept.", category="fact", project=project)
    before = client.get("/memory").text

    _propose(session, project, content="Offered, not kept.", category="decision")
    after = client.get("/memory").text

    assert "waiting on you" not in before.lower()
    assert "waiting on you" in after.lower()


def test_approving_from_the_page_puts_it_in_recall(store):
    from fastapi.testclient import TestClient

    from flanner.web import app

    session, project, repo = store
    outcome = _propose(session, project, content="Use advisory locks.", category="decision")
    poster = TestClient(app, base_url="http://127.0.0.1", follow_redirects=False)

    answer = poster.post(
        "/memory/pending/decide", data={"memory_id": outcome["id"], "decision": "approve"}
    )

    assert answer.status_code == 303
    assert answer.headers["location"] == "/memory/pending"
    assert memory_ops.recall(session, query="advisory locks", project_id=project.id)["memories"]


def test_discarding_from_the_page_removes_it(store):
    from fastapi.testclient import TestClient

    from flanner.web import app

    session, project, repo = store
    outcome = _propose(session, project, content="Use advisory locks.", category="decision")
    poster = TestClient(app, base_url="http://127.0.0.1", follow_redirects=False)

    poster.post("/memory/pending/decide", data={"memory_id": outcome["id"], "decision": "reject"})

    assert memory_ops.pending(session) == []


def test_the_managed_block_tells_an_agent_to_offer_rather_than_save(store):
    """The distinction that makes suggest mode work. An agent that calls
    `remember` for its own conclusions has skipped the policy entirely."""
    session, project, repo = store

    block = agent_hooks.agent_md_block(project)

    assert "memory_consider" in block
    assert "waits for approval" in block
