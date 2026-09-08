"""Artifacts persisted by the write path, and their lineage (PRD §12.4)."""

from pathlib import Path

import pytest

from flanner import artifacts, identity
from flanner.database import (
    artifact_parents,
    create_project,
    get_artifact,
    get_session,
    init_database,
    list_artifacts,
    save_artifact,
)
from flanner.frontmatter import parse_frontmatter
from flanner.plan_ops import create_plan, record_new_version, workspace_id_for
from flanner.reconcile import read_managed


@pytest.fixture
def project(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    session = get_session()
    root = tmp_path / "proj"
    root.mkdir()
    proj = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    return session, proj


def _plan_dir(project):
    return Path(project.project_root) / project.plan_directory


# --- the write path now signs what it writes ---


def test_writing_a_version_stores_a_signed_artifact(project):
    session, proj = project
    plan_file, version = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="test"
    )
    session.commit()

    assert version.artifact_id
    stored = get_artifact(session, version.artifact_id)
    assert stored is not None
    assert stored.artifact_type == artifacts.PLAN_VERSION
    assert stored.plan_file_id == str(plan_file.id)
    assert stored.workspace_id == workspace_id_for(proj)


def test_the_artifact_verifies_against_the_body_on_disk(project):
    session, proj = project
    _, version = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="test"
    )
    session.commit()

    _, body = read_managed(Path(version.file_path).read_text(encoding="utf-8"))
    stored = get_artifact(session, version.artifact_id)
    envelope = artifacts.Artifact(
        artifact_type=stored.artifact_type,
        workspace_id=stored.workspace_id,
        content_hash=stored.content_hash,
        actor_device_id=stored.actor_device_id,
        created_at=stored.created_at,
        artifact_id=stored.artifact_id,
        signature=stored.signature,
        plan_file_id=stored.plan_file_id,
        actor_user_id=stored.actor_user_id,
    )
    verdict = artifacts.verify_artifact(envelope, identity.device_public_key_b64())
    assert verdict, verdict.reason
    # ...and the signed content hash is the body actually on disk.
    assert stored.content_hash == artifacts.hash_text(body)


def test_frontmatter_carries_the_artifact_identity(project):
    session, proj = project
    _, version = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="test"
    )
    session.commit()

    fm, _ = parse_frontmatter(Path(version.file_path).read_text(encoding="utf-8"))
    assert fm["artifact_id"] == version.artifact_id
    assert fm["workspace_id"] == workspace_id_for(proj)
    assert fm["actor_device_id"] == identity.device_id()
    assert "parents" not in fm  # v1 is a root


def test_versions_form_a_signed_chain(project):
    session, proj = project
    plan_file, v1 = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="test"
    )
    session.commit()
    v2 = record_new_version(
        session, project=proj, plan_file=plan_file, content="# two\n", created_by="test", notes=""
    )
    v3 = record_new_version(
        session,
        project=proj,
        plan_file=plan_file,
        content="# three\n",
        created_by="test",
        notes="",
    )

    graph = artifact_parents(session, str(plan_file.id))
    assert graph[v2.artifact_id] == (v1.artifact_id,)
    assert graph[v3.artifact_id] == (v2.artifact_id,)
    # A single, unambiguous history.
    assert artifacts.find_heads(graph) == {v3.artifact_id}
    assert artifacts.is_conflicted(graph) is False
    assert artifacts.is_ancestor(v1.artifact_id, v3.artifact_id, graph)


def test_each_version_gets_its_own_artifact(project):
    session, proj = project
    plan_file, _ = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="test"
    )
    session.commit()
    record_new_version(
        session, project=proj, plan_file=plan_file, content="# two\n", created_by="test", notes=""
    )
    stored = list_artifacts(session, plan_file_id=str(plan_file.id))
    assert len({a.artifact_id for a in stored}) == 2


def test_identical_content_in_two_plans_still_yields_distinct_artifacts(project):
    # Same body, different plan: the envelope differs, so identity does too.
    session, proj = project
    _, a = create_plan(session, project=proj, name="one", content="# same\n", created_by="t")
    session.commit()
    _, b = create_plan(session, project=proj, name="two", content="# same\n", created_by="t")
    session.commit()
    assert a.artifact_id != b.artifact_id


# --- storage behaviour ---


def test_re_receiving_an_artifact_is_a_no_op(project):
    session, proj = project
    _, version = create_plan(session, project=proj, name="arch", content="# one\n", created_by="t")
    session.commit()
    stored = get_artifact(session, version.artifact_id)

    again = save_artifact(
        session,
        artifact_id=stored.artifact_id,
        artifact_type=stored.artifact_type,
        workspace_id=stored.workspace_id,
        content_hash=stored.content_hash,
        actor_device_id=stored.actor_device_id,
        created_at=stored.created_at,
        signature=stored.signature,
    )
    assert again.artifact_id == stored.artifact_id
    assert len(list_artifacts(session)) == 1


def test_artifacts_may_reference_a_plan_this_device_lacks(project):
    # Sync delivers out of order; holding such an artifact is correct.
    session, _ = project
    save_artifact(
        session,
        artifact_id="sha256:orphan",
        artifact_type=artifacts.PLAN_VERSION,
        workspace_id="ws_remote",
        content_hash="sha256:abc",
        actor_device_id="dev_other",
        created_at="2026-01-01T00:00:00Z",
        signature="sig",
        plan_file_id="a-plan-we-have-never-seen",
    )
    assert get_artifact(session, "sha256:orphan") is not None


def test_a_version_predating_artifacts_starts_a_new_root(project):
    """An unsigned older version must not produce a dangling parent link."""
    session, proj = project
    plan_file, v1 = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="t"
    )
    v1.artifact_id = None  # as written by an older flanner
    session.commit()

    v2 = record_new_version(
        session, project=proj, plan_file=plan_file, content="# two\n", created_by="t", notes=""
    )
    graph = artifact_parents(session, str(plan_file.id))
    assert graph[v2.artifact_id] == ()


# --- migration (PRD §24: an existing catalog must upgrade in place) ---


def test_an_existing_v1_database_upgrades(tmp_path):
    from sqlalchemy import text

    from flanner.database import SCHEMA_VERSION, get_db_path

    db_path = tmp_path / "old.db"
    init_database(str(db_path))
    session = get_session()

    # Rewind to the pre-artifact layout an installed user would have. Every
    # column a later migration adds has to go, or that migration re-runs
    # against a schema create_all already brought forward and fails on a
    # duplicate. Leaving one behind is how this test stopped covering the
    # newest migration while still passing for the older ones.
    session.execute(text("DROP INDEX IF EXISTS ix_versions_artifact_id"))
    session.execute(text("ALTER TABLE versions DROP COLUMN artifact_id"))
    session.execute(text("ALTER TABLE projects DROP COLUMN workspace_id"))
    session.execute(text("DROP TABLE artifacts"))
    session.execute(text("PRAGMA user_version = 1"))
    session.commit()
    session.close()

    init_database(str(db_path))  # reopening must migrate, not fail
    session = get_session()
    assert get_db_path() == str(db_path)
    columns = {row[1] for row in session.execute(text("PRAGMA table_info(versions)"))}
    assert "artifact_id" in columns
    assert "workspace_id" in {
        row[1] for row in session.execute(text("PRAGMA table_info(projects)"))
    }
    assert int(session.execute(text("PRAGMA user_version")).scalar()) == SCHEMA_VERSION
    # The new table is back and usable.
    assert list_artifacts(session) == []


def test_a_migration_survives_create_all_getting_there_first(tmp_path):
    """Regression: upgrading from v1 crashed on a duplicate column.

    `_apply_schema` runs `create_all` before the ladder, and `create_all`
    builds any missing table in its present-day shape -- including a column
    a later migration exists to add. A machine upgrading from a version that
    predates `artifacts` therefore gets the table complete, and then the
    migration that adds `memory_id` to it fails.

    Nothing caught this until v4, because it is the first migration to alter
    a table `create_all` might have just made. It would have broken every
    existing install on the release that shipped it.
    """
    from sqlalchemy import text

    db_path = tmp_path / "v1.db"
    init_database(str(db_path))
    session = get_session()
    session.execute(text("DROP INDEX IF EXISTS ix_versions_artifact_id"))
    session.execute(text("ALTER TABLE versions DROP COLUMN artifact_id"))
    session.execute(text("ALTER TABLE projects DROP COLUMN workspace_id"))
    session.execute(text("DROP TABLE artifacts"))
    session.execute(text("PRAGMA user_version = 1"))
    session.commit()
    session.close()

    init_database(str(db_path))

    session = get_session()
    columns = {row[1] for row in session.execute(text("PRAGMA table_info(artifacts)"))}
    assert "memory_id" in columns
    # Once, not twice, and stamped at the top of the ladder.
    assert len([c for c in columns if c == "memory_id"]) == 1
    from flanner.database import SCHEMA_VERSION

    assert int(session.execute(text("PRAGMA user_version")).scalar()) == SCHEMA_VERSION


def test_lineage_survives_the_service_path(db, git_repo, tmp_path, monkeypatch):
    """Regression: the artifact id must be written with the version row.

    Assigning it after create_version's own commit left it unflushed, so the
    create path (which deliberately does not commit) lost the link and the
    next version became a second root instead of a child. Unit tests that
    commit in between hid this; only the real service path showed it.
    """
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    from flanner import services

    proj = services.create_project(name="svc", project_root=str(git_repo))
    plan = services.create_plan_file(proj["id"], "arch", "# one\n")
    services.update_plan_file(plan["id"], "# two\n")
    services.update_plan_file(plan["id"], "# three\n")

    graph = artifact_parents(get_session(), plan["id"])
    assert len(graph) == 3
    heads = artifacts.find_heads(graph)
    assert len(heads) == 1, "versions must form one chain, not several roots"
    assert not artifacts.is_conflicted(graph)
    assert len(artifacts.ancestors(next(iter(heads)), graph)) == 2


# --- what an agent sees over MCP (PRD §20) ---


def test_mcp_history_exposes_artifact_identity_and_lineage(project, monkeypatch):
    session, proj = project
    from flanner import server

    plan_file, v1 = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="claude"
    )
    session.commit()
    v2 = record_new_version(
        session,
        project=proj,
        plan_file=plan_file,
        content="# two\n",
        created_by="claude",
        notes="",
    )

    history = server.get_plan_history_tool(str(plan_file.id))
    by_version = {v["version"]: v for v in history["versions"]}
    assert by_version[1]["artifact_id"] == v1.artifact_id
    assert by_version[2]["parents"] == [v1.artifact_id]
    # An agent can tell whether history is safe to act on.
    assert history["heads"] == [v2.artifact_id]
    assert history["conflicted"] is False


def test_mcp_read_names_the_exact_artifact_it_returned(project):
    session, proj = project
    from flanner import server

    plan_file, version = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="claude"
    )
    session.commit()

    result = server.get_plan_file_tool(str(plan_file.id))
    assert result["version"]["artifact_id"] == version.artifact_id
