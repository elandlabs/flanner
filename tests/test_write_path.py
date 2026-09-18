"""Tests for the locked, atomic plan write path (PRD Phase 1)."""

import os
import time
from pathlib import Path

import pytest

from flanner import storage
from flanner.database import create_plan_file, create_project, get_session
from flanner.exceptions import DatabaseError
from flanner.plan_ops import plan_write_lock, record_new_version, write_version


@pytest.fixture
def plan(db, tmp_path):
    session = get_session()
    root = tmp_path / "proj"
    root.mkdir()
    project = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    plan_file = create_plan_file(session, project_id=project.id, name="alpha")
    write_version(
        session,
        project=project,
        plan_file=plan_file,
        version=1,
        content="# v1\n",
        created_by="test",
        notes="init",
    )
    session.commit()
    return session, project, plan_file


def _plan_dir(project):
    return Path(project.project_root) / project.plan_directory


def test_write_is_atomic_and_leaves_no_temp_files(plan):
    session, project, plan_file = plan
    v1 = _plan_dir(project) / "alpha_v1.md"
    assert v1.exists()
    assert "# v1" in v1.read_text(encoding="utf-8")
    leftovers = list(_plan_dir(project).glob("*.tmp"))
    assert leftovers == []


def test_record_new_version_increments_and_commits(plan):
    session, project, plan_file = plan
    version = record_new_version(
        session,
        project=project,
        plan_file=plan_file,
        content="# v2\n",
        created_by="test",
        notes="",
    )
    assert version.version == 2
    assert plan_file.current_version == 2
    assert (_plan_dir(project) / "alpha_v2.md").exists()
    # v1 untouched
    assert "# v1" in (_plan_dir(project) / "alpha_v1.md").read_text(encoding="utf-8")


def test_refresh_under_lock_sees_concurrent_bump(plan):
    session, project, plan_file = plan
    # Simulate another process having advanced the plan: update the DB behind
    # the object's back and expire it, as if this process's copy were stale.
    from flanner.database import PlanFileModel

    session.query(PlanFileModel).filter_by(id=plan_file.id).update({"current_version": 4})
    session.commit()
    session.expire(plan_file)
    version = record_new_version(
        session,
        project=project,
        plan_file=plan_file,
        content="# v5\n",
        created_by="test",
        notes="",
    )
    assert version.version == 5


def test_never_reuses_version_number_taken_on_disk(plan):
    session, project, plan_file = plan
    # A file for v2 exists on disk but not in the catalog (drift). The writer
    # must skip it, never overwrite.
    stray = _plan_dir(project) / "alpha_v2.md"
    stray.write_text("do not overwrite\n", encoding="utf-8")
    version = record_new_version(
        session,
        project=project,
        plan_file=plan_file,
        content="# next\n",
        created_by="test",
        notes="",
    )
    assert version.version == 3
    assert stray.read_text(encoding="utf-8") == "do not overwrite\n"


def _lock_path(project, plan_id):
    return _plan_dir(project) / f"{storage.LOCK_PREFIX}{plan_id}{storage.LOCK_SUFFIX}"


def test_lock_contention_times_out(plan, monkeypatch):
    """Two writers on the same plan still exclude each other."""
    session, project, plan_file = plan
    monkeypatch.setattr(storage, "LOCK_TIMEOUT_S", 0.3)
    with plan_write_lock(project.project_root, project.plan_directory, plan_file.id):
        with pytest.raises(DatabaseError, match="write lock"):
            with plan_write_lock(project.project_root, project.plan_directory, plan_file.id):
                pass


def test_two_plans_in_one_project_do_not_wait_for_each_other(plan, monkeypatch):
    """The reason the lock is per plan rather than per directory.

    Under the old directory-wide lock this raised: the second writer waited
    on the first and timed out, despite touching a different plan.
    """
    session, project, plan_file = plan
    monkeypatch.setattr(storage, "LOCK_TIMEOUT_S", 0.3)
    other = create_plan_file(session, project_id=project.id, name="other")

    with plan_write_lock(project.project_root, project.plan_directory, plan_file.id):
        with plan_write_lock(project.project_root, project.plan_directory, other.id):
            pass  # no timeout


def _dead_pid() -> int:
    """The id of a process on this machine that has certainly exited."""
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    child.wait()
    return child.pid


def test_a_lock_whose_holder_died_here_is_taken_at_once(plan, monkeypatch):
    """A killed writer's lock no longer blocks every write for 30 seconds.

    Found by the mesh lab: a pull killed with SIGKILL left its lock, and
    the pull retried straight afterwards timed out, because a waiter gives
    up after 10 s and a lock only counted as abandoned after 30.
    """
    session, project, plan_file = plan
    monkeypatch.setattr(storage, "LOCK_TIMEOUT_S", 0.3)
    lock_path = _lock_path(project, plan_file.id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{_dead_pid()} {storage._HOST}", encoding="utf-8")

    with plan_write_lock(project.project_root, project.plan_directory, plan_file.id):
        assert lock_path.read_text(encoding="utf-8").startswith(f"{os.getpid()} ")


def test_a_fresh_lock_from_another_machine_is_still_waited_for(plan, monkeypatch):
    """On a shared drive another machine's process id means nothing here."""
    session, project, plan_file = plan
    monkeypatch.setattr(storage, "LOCK_TIMEOUT_S", 0.3)
    lock_path = _lock_path(project, plan_file.id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(f"{_dead_pid()} some-other-machine", encoding="utf-8")

    with pytest.raises(DatabaseError, match="write lock"):
        with plan_write_lock(project.project_root, project.plan_directory, plan_file.id):
            pass


def test_stale_lock_is_taken_over(plan):
    session, project, plan_file = plan
    lock_path = _lock_path(project, plan_file.id)
    lock_path.write_text("999999", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock_path, (old, old))
    # Acquires despite the existing (stale) lock file
    with plan_write_lock(project.project_root, project.plan_directory, plan_file.id):
        assert lock_path.exists()
    assert not lock_path.exists()
