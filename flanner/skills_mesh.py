"""Sending a skill package to a teammate, and receiving one.

Three states, not one event: a package is **received**, then **verified**,
then — if somebody on this machine says so — **installed**. Collapsing
them would make "did I agree to this?" unanswerable, and it is the one
question that matters when the bytes came from another person.

A bundle carries the package and nothing else. No evidence, no recorded
uses, no session references: those are the private half of Flanner
Skills, and shipping them alongside a skill would turn sharing a useful
procedure into an unannounced disclosure of how somebody works. The
bundle is built from the snapshot store, which only ever holds package
bytes, so this is true by construction rather than by filtering.

The bundle format is a deterministic JSON object — sorted paths, text
where the file is text and base64 where it is not — so the same package
bundles to the same bytes on any machine and the content hash in the
signed envelope means something.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import artifacts, skills_manage, skills_ops
from .database import (
    SkillChannelModel,
    SkillTransferModel,
    save_envelope,
)

RECEIVED = "received"
VERIFIED = "verified"
INSTALLED = "installed"
REJECTED = "rejected"

#: A package much bigger than this is not a skill, and refusing early is
#: cheaper for everybody than discovering it after it has been signed.
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

BUNDLE_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- the bundle ---------------------------------------------------------------


def bundle(manifest_hash: str, skill_name: str, agent: str = "claude-code") -> bytes:
    """A stored snapshot, as bytes that can be signed and sent.

    Built from the snapshot store, which holds package files and nothing
    else. There is no path here that could reach an observation or a piece
    of evidence, which is a stronger guarantee than remembering to leave
    them out.
    """
    if not skills_manage.verify(manifest_hash):
        raise ValueError(f"no verified snapshot for {manifest_hash}")

    root = skills_manage.snapshot_path(manifest_hash)
    files, total, truncated = skills_ops.package_files(root)
    if truncated or total > MAX_BUNDLE_BYTES:
        raise ValueError(f"{skill_name} is too large to send whole")

    entries: dict[str, dict[str, str]] = {}
    for path in files:
        raw = path.read_bytes()
        name = str(path.relative_to(root)).replace("\\", "/")
        try:
            entries[name] = {"text": raw.decode("utf-8")}
        except UnicodeDecodeError:
            entries[name] = {"base64": base64.b64encode(raw).decode("ascii")}

    return json.dumps(
        {
            "bundle_version": BUNDLE_VERSION,
            "skill": skill_name,
            "agent": agent,
            "manifest_hash": manifest_hash,
            "files": entries,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def unbundle(payload: bytes) -> dict[str, Any]:
    """Read a bundle, or say why it cannot be read."""
    try:
        found = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError(f"not a readable skill bundle: {error}") from None
    if not isinstance(found, dict) or found.get("bundle_version") != BUNDLE_VERSION:
        raise ValueError("not a skill bundle this version understands")
    for field in ("skill", "agent", "manifest_hash", "files"):
        if field not in found:
            raise ValueError(f"skill bundle is missing {field}")
    return found


def restore(payload: bytes) -> str:
    """Write a bundle into the snapshot store, and check it is what it claims.

    The hash is recomputed from the written files rather than trusted from
    the bundle. A sender who lies about it — or a bundle corrupted on the
    way — is caught here, before anything can be installed from it.
    """
    found = unbundle(payload)
    claimed = str(found["manifest_hash"])

    staging = skills_manage.store_root() / f"incoming-{uuid.uuid4().hex[:12]}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for name, content in sorted(found["files"].items()):
            target = _safe_path(staging, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if "text" in content:
                target.write_text(content["text"], encoding="utf-8", newline="")
            else:
                target.write_bytes(base64.b64decode(content["base64"]))

        actual, *_ = skills_ops.manifest_hash(staging)
        if actual != claimed:
            raise ValueError(
                f"the package does not hash to what the sender claimed "
                f"({actual} against {claimed}); nothing was stored"
            )
        kept = skills_manage.snapshot(staging)
        return kept.manifest_hash
    finally:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)


def _safe_path(root: Path, name: str) -> Path:
    """A path inside `root`, or a refusal.

    A bundle is bytes from another machine, and `../../.ssh/authorized_keys`
    is a valid-looking entry name. Resolved and checked rather than
    sanitised, because the ways to write an escaping path are more numerous
    than the ways to spot one.
    """
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise ValueError(f"bundle entry {name!r} points outside the package")
    return target


# --- sending ------------------------------------------------------------------


def share(
    session: Session,
    manifest_hash: str,
    skill_name: str,
    workspace_id: str,
    *,
    agent: str = "claude-code",
    signing_key: Any = None,
) -> dict[str, Any]:
    """Sign a package for a workspace and record that it was sent.

    Signing is the whole of sending here: the artifact goes out through the
    same sync everything else uses, so there is no second transport to keep
    in step with this one.
    """
    payload = bundle(manifest_hash, skill_name, agent)
    artifact = artifacts.make_artifact(
        artifact_type=artifacts.SKILL_PACKAGE,
        workspace_id=workspace_id,
        content_hash=artifacts.hash_bytes(payload),
        signing_key=signing_key,
    )
    save_envelope(session, artifact, payload=payload.decode("utf-8"))
    session.commit()
    return {
        "artifact_id": artifact.artifact_id,
        "skill": skill_name,
        "manifest_hash": manifest_hash,
        "workspace_id": workspace_id,
        "bytes": len(payload),
        "carries": "package files only; no evidence, uses or session references",
    }


# --- receiving ----------------------------------------------------------------


def materialise(session: Session, envelope: Any, payload: bytes, workspace_id: str) -> str:
    """Turn a verified package artifact into a transfer waiting on a person.

    Called from the sync ingest path once the signature and the content
    hash have already been checked, so what is left here is whether the
    bundle is readable and whether it hashes to what it claims. Neither
    failure is an exception: a bad package from one teammate must not
    abort a sync carrying good ones.
    """
    artifact_id = str(getattr(envelope, "artifact_id", ""))
    held = session.query(SkillTransferModel).filter_by(artifact_id=artifact_id).one_or_none()
    if held is not None:
        return held.state

    try:
        found = unbundle(payload)
    except ValueError as error:
        session.add(
            SkillTransferModel(
                artifact_id=artifact_id,
                workspace_id=workspace_id,
                skill_name="(unreadable)",
                manifest_hash="",
                from_device=str(getattr(envelope, "actor_device_id", "")),
                state=REJECTED,
                detail=str(error),
            )
        )
        session.commit()
        return REJECTED

    row = SkillTransferModel(
        artifact_id=artifact_id,
        workspace_id=workspace_id,
        skill_name=str(found["skill"]),
        manifest_hash=str(found["manifest_hash"]),
        agent=str(found["agent"]),
        from_device=str(getattr(envelope, "actor_device_id", "")),
        state=RECEIVED,
    )
    try:
        row.manifest_hash = restore(payload)
        row.state = VERIFIED
        row.detail = "the package hashes to what the sender claimed"
    except ValueError as error:
        row.state = REJECTED
        row.detail = str(error)

    session.add(row)
    _note_channel(session, workspace_id, row)
    session.commit()
    return row.state


def _note_channel(session: Session, workspace_id: str, row: SkillTransferModel) -> None:
    """Record a new version against its channel, if one is subscribed.

    Recording only. A subscription that installed would hand whoever
    publishes it the ability to change what an agent on this machine
    reads, which is what every approval in this feature exists to stop.
    """
    channel = (
        session.query(SkillChannelModel)
        .filter_by(workspace_id=workspace_id, name=row.skill_name, subscribed=True)
        .one_or_none()
    )
    if channel is None:
        return
    row.channel = channel.name
    # Not pinned: it arrived because this machine subscribed, so the row is
    # a notice about a new version rather than a copy somebody asked for.
    row.pinned = False
    channel.last_seen_hash = row.manifest_hash
    channel.updated_at = utcnow()


def transfers(session: Session, workspace_id: str | None = None) -> list[dict[str, Any]]:
    """Packages that arrived, newest first, and where each one got to."""
    query = session.query(SkillTransferModel)
    if workspace_id:
        query = query.filter(SkillTransferModel.workspace_id == workspace_id)
    return [
        {
            "id": str(row.id),
            "skill": row.skill_name,
            "manifest_hash": row.manifest_hash,
            "agent": row.agent,
            "from_device": row.from_device,
            "state": row.state,
            "detail": row.detail,
            "channel": row.channel or None,
            "pinned": row.pinned,
            "in_store": bool(row.manifest_hash) and skills_manage.verify(row.manifest_hash),
            "received_at": row.created_at.isoformat() + "Z",
        }
        for row in query.order_by(SkillTransferModel.created_at.desc()).all()
    ]


def install_transfer(
    session: Session,
    transfer_id: str,
    target_root: Path,
    *,
    agent: str = "claude-code",
    project: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """Install a received package, if this machine agrees to.

    Every refusal here is deliberate. A package that did not verify is
    never installed; a package built for another agent is refused rather
    than written into a layout it was not made for; and the target
    directory goes through the same ownership check a local install does,
    so a teammate's package cannot silently replace something somebody
    edited by hand.
    """
    row = _transfer(session, transfer_id)
    if row.state == REJECTED:
        raise ValueError(f"that package was rejected on arrival: {row.detail}")
    if row.state not in (VERIFIED, INSTALLED):
        raise ValueError(f"that package is {row.state}, not verified")
    if row.agent != agent:
        raise ValueError(
            f"that package was built for {row.agent} and this is {agent}. "
            "Skill layouts differ between agents; installing it here would be a guess."
        )
    if not skills_manage.verify(row.manifest_hash):
        raise ValueError("the stored copy no longer hashes to what arrived; nothing was changed")

    target = target_root / ".claude" / "skills" / row.skill_name
    done = skills_manage.install(session, row.manifest_hash, target, agent, project, force=force)
    row.state = INSTALLED
    row.detail = f"installed at {target}"
    row.updated_at = utcnow()
    session.commit()
    return {**done, "skill": row.skill_name, "from_device": row.from_device}


def _transfer(session: Session, transfer_id: str) -> SkillTransferModel:
    try:
        found = (
            session.query(SkillTransferModel)
            .filter_by(id=uuid.UUID(str(transfer_id)))
            .one_or_none()
        )
    except ValueError:
        found = None
    if found is None:
        raise ValueError(f"no transfer with id {transfer_id}")
    return found


# --- channels -----------------------------------------------------------------


def subscribe(session: Session, workspace_id: str, name: str) -> dict[str, Any]:
    """Follow a skill's updates. Notify and review; never install."""
    row = (
        session.query(SkillChannelModel)
        .filter_by(workspace_id=workspace_id, name=name)
        .one_or_none()
    )
    if row is None:
        row = SkillChannelModel(workspace_id=workspace_id, name=name)
        session.add(row)
    row.subscribed = True
    row.updated_at = utcnow()
    session.commit()
    return {
        "name": name,
        "subscribed": True,
        "installs": False,
        "note": "New versions arrive as transfers waiting on you. Nothing installs itself.",
    }


def unsubscribe(session: Session, workspace_id: str, name: str) -> dict[str, Any]:
    row = (
        session.query(SkillChannelModel)
        .filter_by(workspace_id=workspace_id, name=name)
        .one_or_none()
    )
    if row is not None:
        row.subscribed = False
        row.updated_at = utcnow()
        session.commit()
    return {"name": name, "subscribed": False}


def channels(session: Session, workspace_id: str | None = None) -> list[dict[str, Any]]:
    query = session.query(SkillChannelModel)
    if workspace_id:
        query = query.filter(SkillChannelModel.workspace_id == workspace_id)
    return [
        {
            "name": row.name,
            "workspace_id": row.workspace_id,
            "subscribed": row.subscribed,
            "last_seen_hash": row.last_seen_hash or None,
            "installs": False,
        }
        for row in query.order_by(SkillChannelModel.name).all()
    ]
