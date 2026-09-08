"""The memory search index, and the two ways it could quietly not exist.

It is not a `Base` table, because `create_all` cannot make an FTS5 virtual
table. It is not a migration either, and that is the part worth a test: the
schema code stamps a fresh database and returns *before* the migration
ladder runs, so anything registered there would exist on every upgraded
machine and on no new one. That is the worst shape a schema bug can take,
because it only appears for people who have never used the product.

The second way is a Python whose bundled SQLite was built without FTS5.
That must degrade, not crash, and it must say so rather than failing later
at the first search.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text

from flanner import database as db


def _has_index(engine) -> bool:
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT name FROM sqlite_master WHERE name = 'memory_search'")
        ).fetchall()
    return bool(found)


@pytest.fixture(autouse=True)
def _restore_flag():
    """The availability flag is module state, so a failing test must not leak it."""
    before = db.SEARCH_INDEX_AVAILABLE
    yield
    db.SEARCH_INDEX_AVAILABLE = before


def test_a_brand_new_database_has_the_index(tmp_path):
    """The case a migration would have missed, and the reason this is not one."""
    db.init_database(str(tmp_path / "fresh.db"))

    assert _has_index(db._engine)
    assert db.SEARCH_INDEX_AVAILABLE


def test_an_existing_database_gains_it_on_the_next_start(tmp_path):
    """Somebody who has used flanner since before memory existed."""
    path = tmp_path / "existing.db"
    db.init_database(str(path))
    with db._engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE memory_search")
    assert not _has_index(db._engine)

    db.init_database(str(path))

    assert _has_index(db._engine)


def test_starting_twice_is_not_an_error(tmp_path):
    """It runs on every start, so `IF NOT EXISTS` is load-bearing."""
    path = tmp_path / "twice.db"
    db.init_database(str(path))
    db.init_database(str(path))

    assert _has_index(db._engine)


def test_the_index_actually_matches(tmp_path):
    """A table that exists and cannot be searched would pass every test above."""
    db.init_database(str(tmp_path / "match.db"))
    session = db.get_session()
    session.execute(
        text(
            "INSERT INTO memory_search (title, body, source_refs, category, memory_id)"
            " VALUES ('Timestamp convention', 'Use UTC-naive timestamps in SQLite',"
            " '[]', 'decision', 'mem-1')"
        )
    )

    hit = session.execute(
        text("SELECT memory_id FROM memory_search WHERE memory_search MATCH 'timestamps'")
    ).fetchall()

    assert [row[0] for row in hit] == ["mem-1"]


def test_a_build_without_fts5_still_starts(tmp_path, monkeypatch):
    """Degrade to a slower search, never refuse to run.

    Simulated by making the DDL ask for a module that is genuinely absent,
    which produces the same `no such module` the real case does rather than
    a mock that only resembles it.
    """
    monkeypatch.setattr(
        db,
        "SEARCH_INDEX_DDL",
        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts_that_does_not_exist(title)",
    )

    db.init_database(str(tmp_path / "nofts.db"))

    assert db.SEARCH_INDEX_AVAILABLE is False
    assert not _has_index(db._engine)


def test_plans_are_unaffected_by_a_missing_index(tmp_path, monkeypatch):
    """The property that lets this fail softly: nothing about plans uses it."""
    monkeypatch.setattr(
        db,
        "SEARCH_INDEX_DDL",
        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts_that_does_not_exist(title)",
    )
    db.init_database(str(tmp_path / "nofts2.db"))
    session = db.get_session()

    project = db.create_project(
        session, name="p", project_root=str(tmp_path), auto_gitignore=False
    )
    session.commit()

    assert db.list_projects(session)[0].id == project.id


def test_the_flag_recovers_when_the_next_database_can(tmp_path, monkeypatch):
    """It is module state read by recall, so a stale False would silently
    leave a working machine on the slow path forever."""
    monkeypatch.setattr(
        db,
        "SEARCH_INDEX_DDL",
        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_search USING fts_that_does_not_exist(title)",
    )
    db.init_database(str(tmp_path / "broken.db"))
    assert db.SEARCH_INDEX_AVAILABLE is False
    monkeypatch.undo()

    db.init_database(str(tmp_path / "working.db"))

    assert db.SEARCH_INDEX_AVAILABLE is True


def test_this_python_has_fts5_at_all():
    """Not a test of our code. It says which half of the suite is real here,
    so a CI image without FTS5 reports that rather than looking green."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:  # pragma: no cover - depends on the build
        pytest.skip("this Python's SQLite has no FTS5; the fallback path is what runs here")
    finally:
        connection.close()


def test_the_index_is_not_a_migration(tmp_path):
    """If it ever becomes one, the fresh-database path loses it silently.

    `_apply_schema` stamps a fresh database and returns before the ladder
    runs, so an index created by a migration would exist on every upgraded
    machine and on no new one -- a bug that only ever appears for people who
    have never used the product.

    The version this was written against was 3, and asserting that number
    was a way of saying "adding the index did not bump it". A literal is the
    wrong way to say that: it fails on the next honest bump and teaches
    whoever is holding the release to edit the number rather than think.
    """
    import inspect as _inspect

    for version, migration in db.MIGRATIONS.items():
        source = _inspect.getsource(migration)
        assert "memory_search" not in source, f"migration {version} creates the index"
        assert "fts5" not in source.lower(), f"migration {version} creates the index"


def test_creating_it_is_not_a_schema_bump(tmp_path):
    """A virtual table outside the ladder must not disturb the version, or
    every machine would migrate on the release that adds memory."""
    db.init_database(str(tmp_path / "version.db"))

    with db._engine.connect() as conn:
        stamped = conn.exec_driver_sql("PRAGMA user_version").scalar()

    assert stamped == db.SCHEMA_VERSION


def test_an_unused_engine_is_left_alone(tmp_path):
    """`_ensure_search_index` takes a connection, so it cannot be the thing
    that opens one. Called with an engine that has no memory tables, it
    still only adds the index."""
    engine = create_engine(f"sqlite:///{tmp_path / 'bare.db'}")
    with engine.begin() as conn:
        db._ensure_search_index(conn)

    assert _has_index(engine)
