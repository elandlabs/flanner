"""Storing durable context, finding it again, and correcting it.

The core of the memory domain. Scope, deduplication, the lifecycle and the
claim that the files are the record; the search ranking has its own file.
"""

from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from flanner import memory_ops as mem
from flanner.database import (
    NO_PROJECT,
    create_project,
    get_memory,
    get_session,
    init_database,
    list_memories,
)
from flanner.exceptions import NotFoundError, ValidationError
from flanner.utils import utcnow


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A machine with one adopted repository and an empty catalog."""
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


# --- writing -----------------------------------------------------------------


def test_the_file_is_the_record(store):
    """A memory is a Markdown file somebody can read without flanner."""
    session, project, repo = store

    memory, created = mem.remember(
        session,
        content="Use UTC-naive timestamps in SQLite.",
        category="decision",
        project=project,
    )

    assert created
    path = Path(memory.file_path)
    assert path.is_file()
    raw = path.read_text(encoding="utf-8")
    assert "flanner_memory: true" in raw
    assert "Use UTC-naive timestamps in SQLite." in raw
    assert str(memory.id) in raw


def test_project_memory_does_not_live_with_plans(store):
    """`.plans/` is globbed by the reconciler, which would report every
    memory filed there as an orphan, on every run, forever."""
    session, project, repo = store

    memory, _ = mem.remember(session, content="A decision.", category="decision", project=project)

    assert Path(memory.file_path).parent == repo / ".flanner" / "memory"
    assert not (repo / ".plans" / f"{memory.id}.md").exists()


def test_personal_memory_lives_outside_any_repository(store, tmp_path):
    session, project, repo = store

    memory, _ = mem.remember(
        session, content="Prefer blockers over nitpicks.", category="preference", scope="personal"
    )

    assert Path(memory.file_path).is_relative_to(tmp_path / "home")
    assert memory.project_id == NO_PROJECT


def test_remembering_the_same_thing_twice_makes_one_memory(store):
    """Deduplication, and what makes a retried write safe when the reply to
    the first was lost."""
    session, project, repo = store

    first, created_first = mem.remember(
        session, content="The API allows 20 requests a second.", category="fact", project=project
    )
    second, created_second = mem.remember(
        session, content="The API allows 20 requests a second.", category="fact", project=project
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(list_memories(session)) == 1


def test_whitespace_and_line_endings_do_not_make_a_second_memory(store):
    """Two editors on two platforms are not two beliefs."""
    session, project, repo = store

    first, _ = mem.remember(session, content="One claim.", category="fact", project=project)
    second, created = mem.remember(
        session, content="\r\nOne claim.   \r\n\n", category="fact", project=project
    )

    assert created is False
    assert first.id == second.id


def test_the_same_text_in_two_scopes_is_two_memories(store):
    """A personal preference and a project rule can say the same words and
    mean different things."""
    session, project, repo = store

    first, _ = mem.remember(
        session, content="Prefer small diffs.", category="preference", project=project
    )
    second, created = mem.remember(
        session, content="Prefer small diffs.", category="preference", scope="personal"
    )

    assert created is True
    assert first.id != second.id


def test_a_secret_is_refused_and_leaves_nothing_behind(store):
    """The file is written before the row, so a refusal that happened after
    the write would leave an orphan a rebuild would adopt."""
    session, project, repo = store

    with pytest.raises(mem.SecretRejected):
        mem.remember(
            session,
            content="the key is sk_" "live_51H8xQ2KZvKuTb3mNaBcDeFgH",
            category="fact",
            project=project,
        )

    assert list_memories(session) == []
    assert list((repo / ".flanner" / "memory").glob("*.md")) == []


def test_a_body_too_long_to_be_one_claim_is_refused(store):
    """A memory is atomic. Past a couple of paragraphs it is a transcript
    summary, which recall cannot rank and nobody can correct one piece of."""
    session, project, repo = store

    with pytest.raises(ValidationError, match="one durable claim"):
        mem.remember(session, content="x " * 1200, category="fact", project=project)


def test_an_empty_memory_is_refused(store):
    session, project, repo = store

    with pytest.raises(ValidationError, match="needs a body"):
        mem.remember(session, content="   \n\n  ", category="fact", project=project)


def test_project_scope_without_a_project_is_refused(store):
    """Rather than silently filing it as personal, which is where a memory
    goes to be found by the wrong sessions."""
    session, project, repo = store

    with pytest.raises(ValidationError, match="project scope needs a project"):
        mem.remember(session, content="A decision.", category="decision", project=None)


def test_workspace_scope_says_it_is_not_here_yet(store):
    """The scope exists in the schema from the start so that memories
    written now do not need re-classifying when sharing arrives."""
    session, project, repo = store

    with pytest.raises(ValidationError, match="not available yet"):
        mem.remember(
            session, content="Shared.", category="fact", scope="workspace", project=project
        )


def test_an_unknown_category_is_refused(store):
    session, project, repo = store

    with pytest.raises(ValidationError, match="category must be one of"):
        mem.remember(session, content="A thing.", category="musings", project=project)


def test_a_title_is_derived_when_none_is_given(store):
    session, project, repo = store

    memory, _ = mem.remember(
        session,
        content="Use advisory locks instead of Redis. Redis would need a network.",
        category="decision",
        project=project,
    )

    assert memory.title == "Use advisory locks instead of Redis."


def test_a_derived_title_does_not_end_mid_word(store):
    session, project, repo = store

    memory, _ = mem.remember(
        session,
        content="The production deployment pipeline requires a manual approval "
        "step before anything reaches the customer facing environment",
        category="constraint",
        project=project,
    )

    assert len(memory.title) <= 81
    assert not memory.title.rstrip("…").endswith(" ")
    assert memory.title.endswith("…")


# --- lifecycle ---------------------------------------------------------------


def test_superseding_leaves_the_old_one_readable(store):
    """The history of a decision has to survive the decision changing."""
    session, project, repo = store
    original, _ = mem.remember(
        session, content="Use Redis for the queue.", category="decision", project=project
    )

    replacement = mem.supersede(
        session,
        memory_id=original.id,
        content="Use advisory locks; Redis needs a network we cannot assume.",
        project=project,
        reason="offline requirement",
    )

    old = get_memory(session, original.id)
    assert old.status == "superseded"
    assert Path(old.file_path).is_file()
    assert replacement.supersedes_id == original.id
    assert [m.id for m in list_memories(session)] == [replacement.id]


def test_a_correction_identical_to_the_original_is_refused(store):
    session, project, repo = store
    original, _ = mem.remember(session, content="Use Redis.", category="decision", project=project)

    with pytest.raises(ValidationError, match="identical"):
        mem.supersede(session, memory_id=original.id, content="Use Redis.", project=project)


def test_forgetting_stops_recall_and_keeps_the_file(store):
    session, project, repo = store
    memory, _ = mem.remember(session, content="A thing.", category="fact", project=project)

    outcome = mem.forget(session, memory_id=memory.id)

    assert outcome["purged"] is False
    assert get_memory(session, memory.id).status == "forgotten"
    assert Path(memory.file_path).is_file()
    assert list_memories(session) == []


def test_a_forgotten_memory_can_come_back(store):
    session, project, repo = store
    memory, _ = mem.remember(session, content="A thing.", category="fact", project=project)
    mem.forget(session, memory_id=memory.id)

    mem.restore(session, memory_id=memory.id)

    assert get_memory(session, memory.id).status == "active"
    assert [m.id for m in list_memories(session)] == [memory.id]


def test_purging_removes_the_file_and_the_row(store):
    """Append-only lineage is not a reason to refuse somebody erasure of
    their own data."""
    session, project, repo = store
    memory, _ = mem.remember(session, content="A private thing.", category="fact", project=project)
    path = Path(memory.file_path)

    outcome = mem.forget(session, memory_id=memory.id, purge=True)

    assert outcome["purged"] is True
    assert not path.exists()
    assert get_memory(session, memory.id) is None


def test_restoring_something_that_was_replaced_is_refused(store):
    """It was not removed, it was corrected. Restoring it would put two
    contradicting memories into recall at once."""
    session, project, repo = store
    original, _ = mem.remember(session, content="Use Redis.", category="decision", project=project)
    mem.supersede(session, memory_id=original.id, content="Use locks.", project=project)

    with pytest.raises(ValidationError, match="replaced rather than removed"):
        mem.restore(session, memory_id=original.id)


def test_acting_on_a_memory_that_does_not_exist_says_so(store):
    session, project, repo = store

    for call in (
        lambda: mem.forget(session, memory_id=uuid4()),
        lambda: mem.restore(session, memory_id=uuid4()),
        lambda: mem.describe(session, uuid4()),
    ):
        with pytest.raises(NotFoundError):
            call()


def test_expiry_moves_a_memory_out_of_recall(store):
    """Task context should stop being offered; a decision should not."""
    session, project, repo = store
    stale, _ = mem.remember(
        session,
        content="Resume the migration tomorrow.",
        category="task_context",
        project=project,
        expires_at=utcnow() - timedelta(days=1),
    )
    lasting, _ = mem.remember(
        session, content="Use advisory locks.", category="decision", project=project
    )

    moved = mem.expire_due(session)

    assert moved == 1
    assert get_memory(session, stale.id).status == "expired"
    assert get_memory(session, lasting.id).status == "active"


def test_an_expired_memory_is_not_recalled_even_before_it_is_swept(store):
    """Expiry is noticed when it matters rather than by a timer, so recall
    must not depend on the sweep having run."""
    session, project, repo = store
    mem.remember(
        session,
        content="Resume the migration tomorrow.",
        category="task_context",
        project=project,
        expires_at=utcnow() - timedelta(days=1),
    )

    found = mem.recall(session, query="migration", project_id=project.id)

    assert found["memories"] == []


# --- events ------------------------------------------------------------------


def test_what_happened_to_a_memory_is_recorded(store):
    session, project, repo = store
    memory, _ = mem.remember(session, content="A thing.", category="fact", project=project)
    mem.forget(session, memory_id=memory.id)
    mem.restore(session, memory_id=memory.id)

    actions = [e["action"] for e in mem.describe(session, memory.id)["events"]]

    assert actions == ["created", "forgotten", "restored"]


# --- drift -------------------------------------------------------------------


def test_a_missing_file_is_reported(store):
    session, project, repo = store
    memory, _ = mem.remember(session, content="A thing.", category="fact", project=project)
    Path(memory.file_path).unlink()

    kinds = [kind for kind, _id, _detail in mem.drift(session)]

    assert kinds == ["mem_missing_file"]


def test_an_edited_file_is_reported(store):
    """Somebody may edit a memory in their editor. That is allowed, and the
    catalog has to notice rather than quietly serve the old text."""
    session, project, repo = store
    memory, _ = mem.remember(session, content="A thing.", category="fact", project=project)
    path = Path(memory.file_path)
    path.write_text(path.read_text(encoding="utf-8") + "\nAnd another thing.\n", encoding="utf-8")

    kinds = [kind for kind, _id, _detail in mem.drift(session)]

    assert kinds == ["mem_hash_mismatch"]


def test_an_untouched_store_reports_nothing(store):
    session, project, repo = store
    mem.remember(session, content="A thing.", category="fact", project=project)

    assert mem.drift(session) == []
