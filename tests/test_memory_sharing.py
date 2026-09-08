"""Carrying a memory to a teammate's device, and the rules about who may.

The seam where memory stops being local. Everything here is about what may
cross it, what a receiving device does with what arrives, and what it
refuses even though the signature checked out.

What these do NOT cover is a real transfer between two machines. There is
no second device in this suite, so the transport is stubbed and the
end-to-end path stays unproven. That is stated here rather than left to be
discovered, because a green file is easy to mistake for a working feature.
"""

from __future__ import annotations

import subprocess
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import artifacts, identity
from flanner import memory_ops as mem
from flanner.database import ArtifactModel, create_project, get_session, init_database
from flanner.exceptions import NotFoundError, ValidationError

WORKSPACE = "ws_team"


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
    project.workspace_id = WORKSPACE
    session.commit()
    return session, project, home


@pytest.fixture
def peer_key():
    """A teammate's device key. Not this machine's."""
    return Ed25519PrivateKey.generate()


def _theirs(peer_key, *, body: str, memory_id: str, kind: str = artifacts.MEMORY_RECORD):
    """An artifact as another device would have signed it."""
    return artifacts.make_artifact(
        artifact_type=kind,
        workspace_id=WORKSPACE,
        content_hash=artifacts.hash_bytes(body.encode("utf-8")),
        memory_id=memory_id,
        signing_key=peer_key,
    )


# --- what may be shared --------------------------------------------------------


def test_sharing_signs_the_memory_into_the_workspace(store):
    session, project, home = store
    memory, _ = mem.remember(
        session,
        content="Use advisory locks; the tool must work offline.",
        category="decision",
        project=project,
    )

    result = mem.promote(session, memory_id=memory.id, workspace_id=WORKSPACE)

    artifact = session.get(ArtifactModel, result["artifact_id"])
    assert artifact is not None
    assert artifact.artifact_type == artifacts.MEMORY_RECORD
    assert artifact.memory_id == str(memory.id)


def test_personal_memory_can_never_be_shared(store):
    """Acceptance criterion 6. Refused in the one function that could do
    it, so the rule holds however the surfaces above it change."""
    session, project, home = store
    personal, _ = mem.remember(
        session, content="Prefers blockers over nitpicks.", category="preference", scope="personal"
    )

    with pytest.raises(ValidationError, match="personal memory cannot be shared"):
        mem.promote(session, memory_id=personal.id, workspace_id=WORKSPACE)


def test_joining_a_workspace_shares_nothing_on_its_own(store):
    """Somebody wrote those before deciding to work with anybody. Reading a
    later decision backwards onto them would share what nobody offered."""
    session, project, home = store
    mem.remember(session, content="A decision.", category="decision", project=project)
    mem.remember(session, content="A constraint.", category="constraint", project=project)

    assert mem.shared_memories(session, WORKSPACE) == []
    assert session.query(ArtifactModel).count() == 0


def test_a_restricted_memory_stays_on_this_machine(store):
    session, project, home = store
    memory, _ = mem.remember(
        session,
        content="An internal detail.",
        category="fact",
        project=project,
        sensitivity="restricted",
    )

    with pytest.raises(ValidationError, match="restricted"):
        mem.promote(session, memory_id=memory.id, workspace_id=WORKSPACE)


def test_a_forgotten_memory_cannot_be_shared(store):
    session, project, home = store
    memory, _ = mem.remember(session, content="A decision.", category="decision", project=project)
    mem.forget(session, memory_id=memory.id)

    with pytest.raises(ValidationError, match="only an active memory"):
        mem.promote(session, memory_id=memory.id, workspace_id=WORKSPACE)


def test_sharing_something_that_does_not_exist_says_so(store):
    session, project, home = store

    with pytest.raises(NotFoundError):
        mem.promote(session, memory_id=uuid4(), workspace_id=WORKSPACE)


def test_the_signature_covers_the_body_and_not_the_header(store):
    """A header carries a status and a file path that differ per machine.
    Signing it would make a receiving device compute a different hash for
    the same memory and reject work it should accept."""
    session, project, home = store
    body = "Use advisory locks; the tool must work offline."
    memory, _ = mem.remember(session, content=body, category="decision", project=project)

    result = mem.promote(session, memory_id=memory.id, workspace_id=WORKSPACE)

    artifact = session.get(ArtifactModel, result["artifact_id"])
    assert artifact.content_hash == artifacts.hash_bytes(body.encode("utf-8"))


def test_a_correction_points_at_what_it_corrects(store):
    """Lineage per memory, so a receiver can order two versions without
    trusting either device's clock."""
    session, project, home = store
    memory, _ = mem.remember(session, content="Use Redis.", category="decision", project=project)
    first = mem.promote(session, memory_id=memory.id, workspace_id=WORKSPACE)

    second = mem.promote(session, memory_id=memory.id, workspace_id=WORKSPACE)

    artifact = session.get(ArtifactModel, second["artifact_id"])
    assert first["artifact_id"] in artifact.parents


# --- withdrawing ----------------------------------------------------------------


def test_withdrawing_signs_a_tombstone(store):
    session, project, home = store
    memory, _ = mem.remember(session, content="A decision.", category="decision", project=project)
    mem.promote(session, memory_id=memory.id, workspace_id=WORKSPACE)

    result = mem.withdraw(session, memory_id=memory.id, reason="superseded")

    artifact = session.get(ArtifactModel, result["artifact_id"])
    assert artifact.artifact_type == artifacts.MEMORY_TOMBSTONE
    assert "not an erasure" in result["message"]


def test_withdrawing_something_never_shared_is_refused(store):
    """`forget` is the local operation. Offering a tombstone for something
    no peer has would look like it did something and do nothing."""
    session, project, home = store
    memory, _ = mem.remember(session, content="A decision.", category="decision", project=project)

    with pytest.raises(ValidationError, match="never shared"):
        mem.withdraw(session, memory_id=memory.id)


# --- receiving -------------------------------------------------------------------


def test_a_received_memory_becomes_something_this_device_can_recall(store, peer_key):
    """The entry condition the plan names: received artifacts become
    visible local records, not rows nobody ever sees."""
    session, project, home = store
    body = "The staging cluster is rebuilt every Sunday night."
    envelope = _theirs(peer_key, body=body, memory_id=str(uuid4()))

    outcome = mem.materialise(session, envelope=envelope, body=body, workspace_id=WORKSPACE)

    assert outcome["outcome"] == "received"
    found = mem.recall(session, query="staging cluster", project_id=project.id)
    assert [m["id"] for m in found["memories"]] == [outcome["id"]]


def test_a_received_memory_is_written_as_a_file_like_any_other(store, peer_key):
    session, project, home = store
    body = "The staging cluster is rebuilt every Sunday night."
    envelope = _theirs(peer_key, body=body, memory_id=str(uuid4()))

    outcome = mem.materialise(session, envelope=envelope, body=body, workspace_id=WORKSPACE)

    from pathlib import Path

    path = Path(mem.describe(session, UUID(outcome["id"]))["file_path"])
    assert path.is_file()
    assert body in path.read_text(encoding="utf-8")


def test_a_received_memory_says_it_came_from_somewhere_else(store, peer_key):
    """Somebody else's writing, marked as such. A reader deciding whether
    to act on it needs to know it was not written here."""
    session, project, home = store
    body = "The staging cluster is rebuilt every Sunday night."
    envelope = _theirs(peer_key, body=body, memory_id=str(uuid4()))

    outcome = mem.materialise(session, envelope=envelope, body=body, workspace_id=WORKSPACE)

    detail = mem.describe(session, UUID(outcome["id"]))
    assert detail["source_type"] == "imported"
    assert detail["scope"] == "workspace"


def test_a_body_that_does_not_match_the_signature_is_refused(store, peer_key):
    """The signature covers a hash of the body. A body that hashes to
    something else did not come from that signature."""
    session, project, home = store
    envelope = _theirs(peer_key, body="what they signed", memory_id=str(uuid4()))

    outcome = mem.materialise(
        session, envelope=envelope, body="something else entirely", workspace_id=WORKSPACE
    )

    assert outcome["outcome"] == "rejected"
    assert "does not match" in outcome["reason"]


def test_a_credential_from_a_peer_is_still_refused(store, peer_key):
    """A peer's device is not this device's judgement. Accepting something
    because it arrived signed would make the guard decorative."""
    session, project, home = store
    body = "the deploy key is AKIAIOSFODNN7EXAMPLE"
    envelope = _theirs(peer_key, body=body, memory_id=str(uuid4()))

    outcome = mem.materialise(session, envelope=envelope, body=body, workspace_id=WORKSPACE)

    assert outcome["outcome"] == "rejected"
    assert "credential" in outcome["reason"]


def test_a_second_version_updates_rather_than_duplicating(store, peer_key):
    session, project, home = store
    memory_id = str(uuid4())
    mem.materialise(
        session,
        envelope=_theirs(peer_key, body="first", memory_id=memory_id),
        body="first",
        workspace_id=WORKSPACE,
    )

    outcome = mem.materialise(
        session,
        envelope=_theirs(peer_key, body="second", memory_id=memory_id),
        body="second",
        workspace_id=WORKSPACE,
    )

    assert outcome["outcome"] == "updated"
    assert mem.describe(session, UUID(memory_id))["body"] == "second"


def test_a_peers_tombstone_stops_recall_without_deleting(store, peer_key):
    """The honest limit. The bytes are already here, and a design with no
    central copy cannot reach into a disk to remove them."""
    session, project, home = store
    memory_id = str(uuid4())
    body = "The staging cluster is rebuilt every Sunday night."
    mem.materialise(
        session,
        envelope=_theirs(peer_key, body=body, memory_id=memory_id),
        body=body,
        workspace_id=WORKSPACE,
    )

    outcome = mem.materialise(
        session,
        envelope=_theirs(
            peer_key, body=memory_id, memory_id=memory_id, kind=artifacts.MEMORY_TOMBSTONE
        ),
        body="",
        workspace_id=WORKSPACE,
    )

    from pathlib import Path

    assert outcome["outcome"] == "withdrawn"
    assert mem.recall(session, query="staging cluster", project_id=project.id)["memories"] == []
    assert Path(mem.describe(session, UUID(memory_id))["file_path"]).is_file()


def test_a_tombstone_for_something_never_held_is_ignored(store, peer_key):
    session, project, home = store
    memory_id = str(uuid4())

    outcome = mem.materialise(
        session,
        envelope=_theirs(
            peer_key, body=memory_id, memory_id=memory_id, kind=artifacts.MEMORY_TOMBSTONE
        ),
        body="",
        workspace_id=WORKSPACE,
    )

    assert outcome["outcome"] == "ignored"


# --- the path a real transfer takes ---------------------------------------------


def test_a_verified_artifact_becomes_a_memory_without_a_second_call(store, peer_key, monkeypatch):
    """`ingest_artifact` is the one function both pull and push go through.
    Materialising anywhere else means a memory that arrived by push is a row
    nobody ever sees."""
    from flanner import sync

    session, project, home = store
    body = "The staging cluster is rebuilt every Sunday night."
    envelope = _theirs(peer_key, body=body, memory_id=str(uuid4()))

    verdict = sync.ingest_artifact(
        session,
        envelope.to_dict(),
        body.encode("utf-8"),
        lambda _device: identity.public_key_b64(peer_key.public_key()),
    )

    assert verdict
    assert mem.recall(session, query="staging cluster", project_id=project.id)["memories"]


def test_a_memory_a_peer_holds_is_offered_in_the_manifest(store):
    """Otherwise nothing would ever ask for it."""
    from flanner import sync

    session, project, home = store
    memory, _ = mem.remember(session, content="A decision.", category="decision", project=project)
    result = mem.promote(session, memory_id=memory.id, workspace_id=WORKSPACE)

    manifest = sync.build_manifest(session, WORKSPACE)

    assert result["artifact_id"] in manifest.artifact_ids


# --- scope --------------------------------------------------------------------


def test_a_project_that_never_joined_cannot_see_shared_memory(store, peer_key, tmp_path):
    """Workspace memory is scoped by the workspace a project joined, not
    by being on the same machine."""
    session, project, home = store
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    solo = create_project(session, name="solo", project_root=str(elsewhere), auto_gitignore=False)
    session.commit()
    body = "The staging cluster is rebuilt every Sunday night."
    mem.materialise(
        session,
        envelope=_theirs(peer_key, body=body, memory_id=str(uuid4())),
        body=body,
        workspace_id=WORKSPACE,
    )

    assert mem.recall(session, query="staging cluster", project_id=solo.id)["memories"] == []
    assert mem.recall(session, query="staging cluster", project_id=project.id)["memories"]


# --- compatibility ---------------------------------------------------------------


def test_a_plan_artifact_is_byte_for_byte_what_it_was(store):
    """Adding a field to the envelope would rewrite every artifact id ever
    signed, because the id covers the envelope. It is omitted when absent,
    and this is what pins that."""
    plan = artifacts.make_artifact(
        artifact_type=artifacts.PLAN_VERSION,
        workspace_id=WORKSPACE,
        content_hash="sha256:" + "a" * 64,
        plan_file_id="pf-1",
        signing_key=Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
    )

    assert "memory_id" not in plan.to_dict()


def test_a_memory_artifact_carries_the_field(store):
    memo = artifacts.make_artifact(
        artifact_type=artifacts.MEMORY_RECORD,
        workspace_id=WORKSPACE,
        content_hash="sha256:" + "b" * 64,
        memory_id="mem-1",
        signing_key=Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
    )

    assert memo.to_dict()["memory_id"] == "mem-1"
    assert artifacts.Artifact.from_dict(memo.to_dict()).memory_id == "mem-1"


def test_sharing_is_gated_on_its_own_feature(store):
    """A plan-only tier stays possible, and a device that predates memory
    sharing does not have the flag rather than being told it does."""
    from flanner import entitlements

    assert entitlements.MEM_SYNC == "mem_sync"
    assert entitlements.MEM_SYNC != entitlements.TEAM_SYNC
