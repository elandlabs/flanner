"""Tags on memories, and finding the memories related to one.

A tag is a label in the file's header. The header is covered by neither the
content hash nor a shared memory's signature, which is what lets a label
change without making a new version of the memory.
"""

from __future__ import annotations

import subprocess

import pytest
from sqlalchemy import create_engine

from flanner import memory_ops as mem
from flanner.database import (
    _apply_schema,
    create_project,
    get_session,
    init_database,
    list_memories,
    list_memory_events,
)
from flanner.exceptions import ValidationError
from flanner.frontmatter import parse_frontmatter


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
    return session, project


def _save(session, project, content, **kwargs):
    memory, _ = mem.remember(
        session, content=content, category="decision", project=project, **kwargs
    )
    return memory


def _header(memory):
    with open(memory.file_path, encoding="utf-8") as f:
        return parse_frontmatter(f.read())[0]


# --- the rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "tag"),
    [
        ("Auth", "auth"),
        ("  Auth Flow ", "auth-flow"),
        ("area/auth", "area/auth"),
        ("v2_db", "v2_db"),
    ],
)
def test_a_tag_has_one_spelling(raw, tag):
    assert mem.normalise_tag(raw) == tag


@pytest.mark.parametrize("raw", ["", "   ", "-lead", "semi;colon", "é", "x" * 41])
def test_a_tag_that_cannot_be_one_is_refused_with_a_reason(raw):
    with pytest.raises(ValidationError):
        mem.normalise_tag(raw)


def test_duplicates_fold_and_the_count_is_capped():
    assert mem.normalise_tags(["Auth", "auth", "db"]) == ["auth", "db"]
    with pytest.raises(ValidationError, match="at most"):
        mem.normalise_tags([f"t{n}" for n in range(mem.MAX_TAGS + 1)])


# --- writing -----------------------------------------------------------------


def test_tags_given_when_saving_reach_the_file_and_the_row(store):
    session, project = store

    memory = _save(session, project, "Sessions expire after 12 hours.", tags=["Auth", "sessions"])

    assert mem.tags_of(memory) == ["auth", "sessions"]
    assert _header(memory)["tags"] == ["auth", "sessions"]


def test_an_untagged_file_has_no_tags_key(store):
    """Files that never use tags stay byte-for-byte what they were."""
    session, project = store

    assert "tags" not in _header(_save(session, project, "Plain memory."))


def test_saving_the_same_text_again_adds_the_new_tags(store):
    session, project = store
    first = _save(session, project, "Use UTC everywhere.", tags=["time"])

    again, created = mem.remember(
        session,
        content="Use UTC everywhere.",
        category="decision",
        project=project,
        tags=["db"],
    )

    assert not created and again.id == first.id
    assert mem.tags_of(again) == ["time", "db"]


def test_retagging_changes_the_label_and_nothing_the_memory_says(store):
    session, project = store
    memory = _save(session, project, "Billing runs nightly.", tags=["billing", "old"])
    digest, body = memory.content_hash, memory.body

    after = mem.retag(session, memory_id=memory.id, add=["jobs"], remove=["old"])

    assert after.id == memory.id
    assert (after.content_hash, after.body) == (digest, body)
    assert mem.tags_of(after) == ["billing", "jobs"]
    assert _header(after)["tags"] == ["billing", "jobs"]
    event = list_memory_events(session, memory.id)[-1]
    assert event.action == "retagged"


def test_retagging_with_no_change_records_nothing(store):
    session, project = store
    memory = _save(session, project, "Nothing to change.", tags=["x"])
    before = len(list_memory_events(session, memory.id))

    mem.retag(session, memory_id=memory.id, add=["x"])

    assert len(list_memory_events(session, memory.id)) == before


def test_a_forgotten_memory_is_not_retagged(store):
    session, project = store
    memory = _save(session, project, "Soon forgotten.")
    mem.forget(session, memory_id=memory.id)

    with pytest.raises(ValidationError, match="forgotten"):
        mem.retag(session, memory_id=memory.id, add=["x"])


def test_a_correction_keeps_the_tags_unless_told_otherwise(store):
    session, project = store
    old = _save(session, project, "Deploy on Fridays.", tags=["deploy"])

    kept = mem.supersede(
        session, memory_id=old.id, content="Never deploy on Fridays.", project=project
    )
    changed = mem.supersede(
        session,
        memory_id=kept.id,
        content="Deploy Tuesday to Thursday.",
        project=project,
        tags=["release"],
    )

    assert mem.tags_of(kept) == ["deploy"]
    assert mem.tags_of(changed) == ["release"]


def test_a_suggestion_carries_its_tags(store):
    session, project = store
    policy = mem.policy_for(project)

    [outcome] = mem.consider(
        session,
        [mem.Candidate(content="The queue is at-least-once.", category="fact", tags=("queue",))],
        policy=policy,
        project=project,
    )

    assert outcome["outcome"] in ("committed", "proposed"), outcome
    [memory] = list_memories(session, status=None)
    assert mem.tags_of(memory) == ["queue"]


# --- reading -----------------------------------------------------------------


def test_recall_can_be_narrowed_to_memories_carrying_every_tag(store):
    session, project = store
    _save(session, project, "Login tokens rotate daily.", tags=["auth", "tokens"])
    _save(session, project, "Login page uses the shared layout.", tags=["ui"])

    result = mem.recall(session, query="login", project_id=project.id, tags=["auth"])

    assert [m["tags"] for m in result["memories"]] == [["auth", "tokens"]]


def test_a_plain_search_finds_a_memory_by_its_tag(store):
    session, project = store
    _save(session, project, "Rotate the signing key yearly.", tags=["kms"])

    result = mem.recall(session, query="kms", project_id=project.id)

    assert len(result["memories"]) == 1


def test_list_filters_by_tag(store):
    session, project = store
    _save(session, project, "One.", tags=["a", "b"])
    _save(session, project, "Two.", tags=["a"])

    assert len(list_memories(session, tags=["a"])) == 2
    assert len(list_memories(session, tags=["a", "b"])) == 1
    assert list_memories(session, tags=["missing"]) == []


def test_related_says_why_each_memory_is_there(store):
    session, project = store
    anchor = _save(
        session,
        project,
        "Auth uses JWT.",
        tags=["auth", "security"],
        source_refs=["file:src/auth.py"],
    )
    same_file = _save(
        session, project, "Auth module has no retries.", source_refs=["file:src/auth.py"]
    )
    two_tags = _save(session, project, "Pen test yearly.", tags=["auth", "security"])
    one_tag = _save(session, project, "Security reviews on Mondays.", tags=["security"])
    _save(session, project, "Unrelated.", tags=["ui"])
    successor = mem.supersede(
        session, memory_id=anchor.id, content="Auth uses PASETO.", project=project
    )

    found = mem.related(session, anchor.id)

    assert [f["id"] for f in found] == [
        str(successor.id),
        str(same_file.id),
        str(two_tags.id),
        str(one_tag.id),
    ]
    assert found[0]["why"] == "it replaced this memory"
    assert found[1]["why"] == "same source: file:src/auth.py"
    assert found[2]["why"] == "shares tags: auth, security"


def test_related_never_offers_another_projects_memory(store, tmp_path):
    session, project = store
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = create_project(
        session, name="other", project_root=str(other_root), auto_gitignore=False
    )
    anchor = _save(session, project, "Here.", tags=["shared-word"])
    _save(session, other, "There.", tags=["shared-word"])

    assert mem.related(session, anchor.id) == []


def test_tags_in_use_are_counted_most_used_first(store):
    session, project = store
    _save(session, project, "One.", tags=["b", "a"])
    _save(session, project, "Two.", tags=["a"])

    assert mem.tags_in_use(session, project_id=project.id) == [
        {"tag": "a", "count": 2},
        {"tag": "b", "count": 1},
    ]


# --- the files stay the record ----------------------------------------------


def test_a_tag_added_by_hand_is_picked_up_by_a_rebuild(store):
    session, project = store
    memory = _save(session, project, "Edited by hand later.")
    with open(memory.file_path, encoding="utf-8") as f:
        raw = f.read()
    with open(memory.file_path, "w", encoding="utf-8") as f:
        f.write(
            raw.replace(
                "flanner_memory: true\n", "flanner_memory: true\ntags:\n- Ops\n- 'bad tag;'\n", 1
            )
        )

    mem.rebuild(session, projects=[project])

    session.refresh(memory)
    assert mem.tags_of(memory) == ["ops"]


def test_an_existing_store_gains_the_tags_column(tmp_path):
    """A version 4 database, upgraded, has every memory untagged."""
    import flanner.database as dbmod

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    try:
        _apply_schema(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE memories DROP COLUMN tags")
            conn.exec_driver_sql("PRAGMA user_version = 4")
        _apply_schema(engine)
        with engine.connect() as conn:
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(memories)")}
            version = conn.exec_driver_sql("PRAGMA user_version").scalar()
        assert "tags" in columns
        assert version == dbmod.SCHEMA_VERSION
    finally:
        engine.dispose()
