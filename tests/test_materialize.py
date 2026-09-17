"""Turning a peer's verified artifacts into working files (PRD §12.4, §14)."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from flanner import artifacts, identity, sync
from flanner.database import (
    Base,
    create_project,
    get_plan_file,
    get_session,
    list_versions,
)
from flanner.frontmatter import read_managed
from flanner.plan_ops import (
    create_plan,
    materialize_version,
    record_new_version,
    workspace_id_for,
)


def _catalog(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture
def sender(db, tmp_path, monkeypatch):
    """A device that authors plans, using the singleton session."""
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "sender-home"))
    session = get_session()
    root = tmp_path / "sender"
    root.mkdir()
    project = create_project(session, name="src", project_root=str(root), auto_gitignore=False)
    return session, project


@pytest.fixture
def receiver(tmp_path):
    """A second device with its own catalog and working tree.

    Not a second *identity*: these fixtures share one process and therefore
    one device key. Tests that turn on "did this device write it?" set that
    up themselves — patching it here signed the sender's artifacts as the
    receiver and broke key resolution for every sync test.
    """
    session = _catalog(tmp_path / "receiver.db")
    root = tmp_path / "receiver"
    root.mkdir()
    project = create_project(session, name="dst", project_root=str(root), auto_gitignore=False)
    return session, project


def _transfer(sender, artifact_id):
    """Everything the wire would carry for one artifact."""
    session, _ = sender
    return sync.LocalPeer(session).fetch([artifact_id])[0]


def _dir(project):
    return Path(project.project_root) / project.plan_directory


# --- the body now actually crosses the wire ---


def test_a_plan_version_travels_with_its_file(sender):
    session, project = sender
    _, version = create_plan(
        session, project=project, name="arch", content="# body\n", created_by="alice"
    )
    session.commit()

    envelope, payload = _transfer(sender, version.artifact_id)
    assert payload is not None, "the body must cross the wire, not just the envelope"
    _, body = read_managed(payload.decode())
    assert body == "# body\n"
    assert sync.payload_digest(envelope["artifact_type"], payload) == envelope["content_hash"]


# --- materialization ---


def test_a_synced_version_becomes_a_real_file(sender, receiver):
    s_session, s_project = sender
    r_session, r_project = receiver
    plan_file, version = create_plan(
        s_session, project=s_project, name="arch", content="# body\n", created_by="alice"
    )
    s_session.commit()
    envelope, payload = _transfer(sender, version.artifact_id)

    result = materialize_version(
        r_session, project=r_project, envelope=envelope, managed_file=payload
    )

    assert result.ok, result.reason
    written = Path(result.version.file_path)
    assert written.exists()
    fm, body = read_managed(written.read_text(encoding="utf-8"))
    assert body == "# body\n"  # byte-identical, so the signature still holds
    # Identity is preserved...
    assert fm["artifact_id"] == version.artifact_id
    assert fm["plan_file_id"] == str(plan_file.id)
    assert fm["plan_name"] == "arch"
    assert fm["created_by"] == "alice"
    # ...while the project fields are the receiver's own.
    assert fm["project_name"] == "dst"
    assert fm["project_id"] == str(r_project.id)


def test_the_materialized_body_still_verifies_against_the_signature(sender, receiver):
    s_session, s_project = sender
    r_session, r_project = receiver
    _, version = create_plan(
        s_session, project=s_project, name="arch", content="# body\n", created_by="alice"
    )
    s_session.commit()
    envelope, payload = _transfer(sender, version.artifact_id)
    materialize_version(r_session, project=r_project, envelope=envelope, managed_file=payload)

    written = Path(
        r_session.query(type(version)).filter_by(artifact_id=version.artifact_id).one().file_path
    )
    _, body = read_managed(written.read_text(encoding="utf-8"))
    assert artifacts.hash_text(body) == envelope["content_hash"]


def test_the_plan_keeps_its_identity_across_devices(sender, receiver):
    s_session, s_project = sender
    r_session, r_project = receiver
    plan_file, version = create_plan(
        s_session, project=s_project, name="arch", content="# body\n", created_by="alice"
    )
    s_session.commit()
    envelope, payload = _transfer(sender, version.artifact_id)
    materialize_version(r_session, project=r_project, envelope=envelope, managed_file=payload)

    assert get_plan_file(r_session, plan_file.id) is not None


def test_materializing_twice_is_a_no_op(sender, receiver):
    s_session, s_project = sender
    r_session, r_project = receiver
    _, version = create_plan(
        s_session, project=s_project, name="arch", content="# body\n", created_by="alice"
    )
    s_session.commit()
    envelope, payload = _transfer(sender, version.artifact_id)

    first = materialize_version(
        r_session, project=r_project, envelope=envelope, managed_file=payload
    )
    second = materialize_version(
        r_session, project=r_project, envelope=envelope, managed_file=payload
    )
    assert first.ok and second.ok
    assert second.reason == "already materialized"
    assert len(list(_dir(r_project).glob("*.md"))) == 1


def test_a_whole_history_materializes_in_order(sender, receiver):
    s_session, s_project = sender
    r_session, r_project = receiver
    plan_file, v1 = create_plan(
        s_session, project=s_project, name="arch", content="# one\n", created_by="alice"
    )
    s_session.commit()
    v2 = record_new_version(
        s_session,
        project=s_project,
        plan_file=plan_file,
        content="# two\n",
        created_by="alice",
        notes="",
    )

    for version in (v1, v2):
        envelope, payload = _transfer(sender, version.artifact_id)
        assert materialize_version(
            r_session, project=r_project, envelope=envelope, managed_file=payload
        ).ok

    versions = list_versions(r_session, plan_file.id)
    assert {v.version for v in versions} == {1, 2}
    assert get_plan_file(r_session, plan_file.id).current_version == 2


# --- refusals ---


def test_a_tampered_body_is_refused(sender, receiver):
    s_session, s_project = sender
    r_session, r_project = receiver
    _, version = create_plan(
        s_session, project=s_project, name="arch", content="# body\n", created_by="alice"
    )
    s_session.commit()
    envelope, payload = _transfer(sender, version.artifact_id)
    tampered = payload.replace(b"# body", b"# rewritten")

    result = materialize_version(
        r_session, project=r_project, envelope=envelope, managed_file=tampered
    )
    assert not result.ok
    assert "does not match the signed content hash" in result.reason
    assert list(_dir(r_project).glob("*.md")) == []


def test_an_unrelated_plan_of_the_same_name_is_not_fused(sender, receiver):
    s_session, s_project = sender
    r_session, r_project = receiver
    # The receiver already has its own, different plan called "arch".
    create_plan(r_session, project=r_project, name="arch", content="# mine\n", created_by="bob")
    r_session.commit()

    _, version = create_plan(
        s_session, project=s_project, name="arch", content="# theirs\n", created_by="alice"
    )
    s_session.commit()
    envelope, payload = _transfer(sender, version.artifact_id)

    result = materialize_version(
        r_session, project=r_project, envelope=envelope, managed_file=payload
    )
    assert not result.ok
    assert "already exists here" in result.reason


def test_a_concurrent_version_number_is_kept_beside_the_local_one(sender, receiver):
    """Two devices used v2 for different content: both survive (PRD §12.4)."""
    s_session, s_project = sender
    r_session, r_project = receiver
    plan_file, v1 = create_plan(
        s_session, project=s_project, name="arch", content="# one\n", created_by="alice"
    )
    s_session.commit()
    envelope1, payload1 = _transfer(sender, v1.artifact_id)
    materialize_version(r_session, project=r_project, envelope=envelope1, managed_file=payload1)

    # Each side writes its own v2.
    remote_v2 = record_new_version(
        s_session,
        project=s_project,
        plan_file=plan_file,
        content="# theirs\n",
        created_by="alice",
        notes="",
    )
    local_plan = get_plan_file(r_session, plan_file.id)
    record_new_version(
        r_session,
        project=r_project,
        plan_file=local_plan,
        content="# mine\n",
        created_by="bob",
        notes="",
    )

    envelope2, payload2 = _transfer(sender, remote_v2.artifact_id)
    result = materialize_version(
        r_session, project=r_project, envelope=envelope2, managed_file=payload2
    )

    assert result.ok
    assert result.conflict_path is not None, "the incoming v2 must not overwrite the local one"
    assert "conflict" in Path(result.conflict_path).name
    # Both bodies are still on disk.
    bodies = {read_managed(p.read_text(encoding="utf-8"))[1] for p in _dir(r_project).glob("*.md")}
    assert {"# one\n", "# mine\n", "# theirs\n"} == bodies


def test_undecodable_payload_is_refused(sender, receiver):
    s_session, s_project = sender
    r_session, r_project = receiver
    _, version = create_plan(
        s_session, project=s_project, name="arch", content="# body\n", created_by="alice"
    )
    s_session.commit()
    envelope, _ = _transfer(sender, version.artifact_id)

    result = materialize_version(
        r_session, project=r_project, envelope=envelope, managed_file=b"\xff\xfe binary"
    )
    assert not result.ok


# --- end to end: sync then materialize ---


def test_sync_then_materialize_reproduces_the_plan_on_the_other_device(sender, receiver, tmp_path):
    s_session, s_project = sender
    r_session, r_project = receiver
    plan_file, v1 = create_plan(
        s_session, project=s_project, name="arch", content="# one\n", created_by="alice"
    )
    s_session.commit()
    record_new_version(
        s_session,
        project=s_project,
        plan_file=plan_file,
        content="# two\n",
        created_by="alice",
        notes="",
    )

    workspace = workspace_id_for(s_project)
    registry = {identity.device_id(): identity.device_public_key_b64()}.get
    report = sync.sync_from_peer(r_session, sync.LocalPeer(s_session), workspace, registry)
    assert report.ok and len(report.accepted) == 2

    peer = sync.LocalPeer(s_session)
    for artifact_id in report.accepted:
        envelope, payload = peer.fetch([artifact_id])[0]
        assert materialize_version(
            r_session, project=r_project, envelope=envelope, managed_file=payload
        ).ok

    on_disk = sorted(p.name for p in _dir(r_project).glob("*.md"))
    assert on_disk == ["arch_v1.md", "arch_v2.md"]


def test_a_pushed_plan_becomes_a_file_too(sender, receiver):
    """A device that is pushed to may never pull, so it cannot wait for one.

    The push path stored the version and stopped there: the artifact was
    held, no file was written, and the plan did not appear in `flanner
    plans` until some later pull happened to materialise it.
    """
    from flanner import push

    s_session, s_project = sender
    r_session, r_project = receiver
    s_project.workspace_id = r_project.workspace_id = "ws_shared"  # one team, two checkouts
    s_session.commit()
    r_session.commit()
    _, version = create_plan(
        s_session, project=s_project, name="arch", content="# pushed\n", created_by="alice"
    )
    s_session.commit()
    envelope, payload = _transfer(sender, version.artifact_id)

    report = push.accept(
        r_session,
        [{"envelope": envelope, "payload": payload.decode("utf-8")}],
        workspace_id=envelope["workspace_id"],
        role="maintainer",
        resolve_key={identity.device_id(): identity.device_public_key_b64()}.get,
    )

    assert report.accepted == [version.artifact_id], report.rejected
    assert sorted(p.name for p in _dir(r_project).glob("*.md")) == ["arch_v1.md"]
    assert get_plan_file(r_session, str(envelope["plan_file_id"])) is not None
