"""The demo catalog is what the browser suite asserts against.

If this drifts, every browser failure is about the seeder rather than about
the UI — which is the expensive kind of red build. So the catalog's shape is
pinned here, in the suite that runs everywhere.
"""

from __future__ import annotations

import pytest

from flanner import demo, review
from flanner.database import get_session, list_plan_files, list_projects, list_versions
from flanner.memory_ops import get_memory


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    home = tmp_path_factory.mktemp("demo-home") / "home"
    manifest = demo.seed(home)
    return home, manifest


def test_the_manifest_names_everything_it_built(seeded):
    home, manifest = seeded
    assert set(manifest["projects"]) == {"payments-service", "notifications-service"}
    assert set(manifest["plans"]) == {
        "jwt-key-rotation",
        "webhook-delivery",
        "idempotency-keys",
        "rate-limiting",
    }
    assert set(manifest["memories"]) == {"kept", "shared", "pending"}
    assert demo.load_manifest(home) == manifest


def test_two_projects_and_four_plans_land_in_the_catalog(seeded):
    _, manifest = seeded
    session = get_session()
    projects = {p.name: p for p in list_projects(session)}
    assert set(projects) == {"payments-service", "notifications-service"}

    names = {
        plan.name for project in projects.values() for plan in list_plan_files(session, project.id)
    }
    assert names == set(manifest["plans"])


def test_one_plan_carries_five_versions(seeded):
    from uuid import UUID

    _, manifest = seeded
    session = get_session()
    versions = list_versions(session, UUID(manifest["plans"]["jwt-key-rotation"]))
    assert sorted(v.version for v in versions) == [1, 2, 3, 4, 5]
    newest = max(versions, key=lambda v: v.version)
    assert newest.notes == "say how to roll back"


def test_the_stale_plan_cites_a_path_git_history_shows_was_removed(seeded):
    """Absence alone proves nothing to `freshness`; a deletion does.

    This is the one part of the catalog that depends on git history rather
    than on rows, so it is the part most likely to be broken by a change
    nobody connected to it.
    """
    from flanner import freshness

    home, manifest = seeded
    root = manifest["projects"]["payments-service"]["root"]
    verdict = freshness.compute_freshness(root, demo._WEBHOOK_PLAN, None, use_cache=False)
    assert verdict["status"] == "stale"
    assert verdict["invalid_refs"] == [demo._MOVED_FILE]


def test_one_plan_is_retired_and_one_is_in_review(seeded):
    from uuid import UUID

    from flanner.assurance import retirement
    from flanner.database import get_plan_file, get_project

    _, manifest = seeded
    session = get_session()

    standing = retirement(session, manifest["plans"]["idempotency-keys"])
    assert standing.retired
    assert standing.reason == "the gateway deduplicates now"

    notifications = get_project(session, UUID(manifest["projects"]["notifications-service"]["id"]))
    reviewed = get_plan_file(session, UUID(manifest["plans"]["rate-limiting"]))
    state = review.status(session, plan_file=reviewed, project=notifications, roles=demo._ROLES)
    # One proposal, and it carries the approval the seeder recorded.
    assert len(state.proposals) == 1
    assert state.accepted_artifact_id is not None


def test_three_memories_with_one_shared_and_one_waiting(seeded):
    from uuid import UUID

    _, manifest = seeded
    session = get_session()
    kept = get_memory(session, UUID(manifest["memories"]["kept"]))
    shared = get_memory(session, UUID(manifest["memories"]["shared"]))
    pending = get_memory(session, UUID(manifest["memories"]["pending"]))

    assert kept.status == "active"
    assert shared.status == "active"
    assert shared.workspace_id == "ws_demo"
    assert pending.status == "proposed"


def test_seeding_over_a_home_it_did_not_write_is_refused(tmp_path):
    """It deletes the home before rebuilding it. That has to be narrow."""
    home = tmp_path / "somebody-elses"
    home.mkdir()
    (home / "data.db").write_bytes(b"not ours")
    with pytest.raises(ValueError, match="not empty"):
        demo.seed(home)
    assert (home / "data.db").exists()
