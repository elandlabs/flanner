"""Taking a skill package under management, installing it, putting it back.

Everything a scan reads belongs to somebody else — a plugin, a
marketplace, the person who wrote it — and flanner never edits those in
place. This module is the other half: bytes flanner itself is
responsible for.

The store is content-addressed. A snapshot lives at
`<flanner home>/skills/snapshots/<digest>`, so its name is a claim about
its contents that can be checked, and two identical packages are one
directory rather than two copies that can drift apart.

An install is written beside the target and swapped in, and the bytes it
replaces are snapshotted first. That is what makes PKG-01's promise
keepable: a failed install leaves the previous version recoverable,
because the previous version was put somewhere recoverable before
anything was touched.
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import skills_ops
from .database import (
    ProjectModel,
    SkillInstallationModel,
    get_project_by_root,
)

#: Ownership of a directory an install would write to.
FLANNER = "flanner"
EXTERNAL = "external"

INSTALLED = "installed"
FAILED = "failed"
REPLACED = "replaced"


class ConflictError(Exception):
    """The target holds bytes flanner did not put there."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def store_root() -> Path:
    """Where snapshots live. Honours FLANNER_HOME like everything else."""
    home = os.environ.get("FLANNER_HOME")
    base = Path(home) if home else Path.home() / ".flanner"
    return base / "skills" / "snapshots"


def snapshot_path(manifest_hash: str) -> Path:
    return store_root() / manifest_hash.replace("sha256:", "")


@dataclass(frozen=True)
class Snapshot:
    """One package's bytes, kept where flanner can put them back."""

    manifest_hash: str
    path: Path
    file_count: int
    size_bytes: int
    created: bool


# --- the store ----------------------------------------------------------------


def snapshot(directory: Path) -> Snapshot:
    """Copy a package into the store, or recognise one already there.

    Copied file by file through the same walk that hashes it, so what is
    stored is exactly what was hashed. A `rglob` copy would also pick up
    the ignored directories — `.git`, `node_modules` — that the hash
    deliberately steps over, and the snapshot would then not match its own
    name.
    """
    digest, count, size, truncated = skills_ops.manifest_hash(directory)
    if truncated:
        raise ValueError(
            f"{directory} is too large to snapshot whole; its hash covers only part of it"
        )

    target = snapshot_path(digest)
    if target.is_dir():
        return Snapshot(digest, target, count, size, created=False)

    # Built beside the final name and moved into place, so a snapshot
    # directory never exists half-written under a name that claims to be
    # a complete copy.
    staging = target.with_name(target.name + f".partial-{uuid.uuid4().hex[:8]}")
    try:
        files, _total, _cut = skills_ops.package_files(directory)
        for source in files:
            destination = staging / source.relative_to(directory)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(target)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return Snapshot(digest, target, count, size, created=True)


def verify(manifest_hash: str) -> bool:
    """Whether the snapshot on disk still hashes to the name it is under."""
    path = snapshot_path(manifest_hash)
    if not path.is_dir():
        return False
    digest, *_ = skills_ops.manifest_hash(path)
    return digest == manifest_hash


# --- installation -------------------------------------------------------------


def ownership(session: Session, target: Path) -> str:
    """Whether flanner put the bytes currently at this path there.

    An install this machine performed and has not since replaced makes the
    directory flanner's; anything else is somebody else's, including a
    directory flanner installed and a person then edited by hand — the
    hash on record no longer matches, and overwriting an edit somebody
    made is exactly the failure worth refusing.
    """
    if not target.exists():
        return FLANNER
    row = (
        session.query(SkillInstallationModel)
        .filter_by(target_path=str(target), status=INSTALLED)
        .one_or_none()
    )
    if row is None:
        return EXTERNAL
    digest, *_ = skills_ops.manifest_hash(target)
    return FLANNER if digest == row.observed_hash else EXTERNAL


def install(
    session: Session,
    manifest_hash: str,
    target: Path,
    agent: str = "claude-code",
    project: ProjectModel | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Put a stored snapshot at a target path, reversibly.

    Order matters and is the whole design: check the conflict, snapshot
    what is there now, stage the new bytes beside it, swap, and only then
    throw the old copy away. Interrupted at any point, either the old
    directory is still in place or it is sitting in the store under a hash
    the returned record names.
    """
    if not verify(manifest_hash):
        raise ValueError(f"no verified snapshot for {manifest_hash}")

    held = ownership(session, target)
    if held == EXTERNAL and not force:
        raise ConflictError(
            f"{target} was not installed by flanner, or has been edited since. "
            "Nothing was changed."
        )

    replaced: str | None = None
    if target.exists():
        replaced = snapshot(target).manifest_hash
        if replaced == manifest_hash:
            return _record(session, manifest_hash, target, agent, project, replaced, changed=False)

    staging = target.with_name(target.name + f".flanner-new-{uuid.uuid4().hex[:8]}")
    previous = target.with_name(target.name + f".flanner-old-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copytree(snapshot_path(manifest_hash), staging)
        if target.exists():
            target.rename(previous)
        staging.rename(target)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        if previous.exists() and not target.exists():
            previous.rename(target)
        _record(session, manifest_hash, target, agent, project, replaced, failed=True)
        raise

    shutil.rmtree(previous, ignore_errors=True)
    return _record(session, manifest_hash, target, agent, project, replaced, changed=True)


def _record(
    session: Session,
    manifest_hash: str,
    target: Path,
    agent: str,
    project: ProjectModel | None,
    replaced: str | None,
    changed: bool = True,
    failed: bool = False,
) -> dict[str, Any]:
    """Close out any earlier install at this path and write the new one."""
    for old in (
        session.query(SkillInstallationModel)
        .filter_by(target_path=str(target), status=INSTALLED)
        .all()
    ):
        old.status = REPLACED
        old.updated_at = utcnow()

    row = SkillInstallationModel(
        agent=agent,
        project_id=project.id if project else None,
        target_path=str(target),
        manifest_hash=manifest_hash,
        observed_hash=manifest_hash if not failed else "",
        replaced_hash=replaced or "",
        ownership=FLANNER,
        status=FAILED if failed else INSTALLED,
    )
    session.add(row)
    session.commit()
    return {
        "installation_id": str(row.id),
        "manifest_hash": manifest_hash,
        "target": str(target),
        "replaced_hash": replaced,
        "changed": changed,
        "status": row.status,
    }


def adopt(
    session: Session,
    name: str,
    project_root: Path | None = None,
    agent: str = "claude-code",
) -> dict[str, Any]:
    """Take a copy of a discovered package into the store.

    A copy, not a move. The package stays where its owner put it, and the
    stored bytes are what a later install or rollback puts back — so
    adopting cannot break the thing it is adopting.
    """
    copies = [p for p in skills_ops.scan(project_root, agent) if p.name == name and p.effective]
    if len(copies) != 1:
        raise ValueError(
            f"{name} does not resolve to exactly one loaded package; "
            "run `flanner skills inspect` to see the copies"
        )
    kept = snapshot(Path(copies[0].directory))
    return {
        "skill": name,
        "manifest_hash": kept.manifest_hash,
        "snapshot": str(kept.path),
        "files": kept.file_count,
        "size_bytes": kept.size_bytes,
        "new": kept.created,
        "source": copies[0].directory,
    }


def rollback(session: Session, installation_id: str, to_hash: str | None = None) -> dict[str, Any]:
    """Put back the bytes an install replaced, or a named earlier snapshot."""
    row = (
        session.query(SkillInstallationModel)
        .filter_by(id=uuid.UUID(installation_id))
        .one_or_none()
    )
    if row is None:
        raise ValueError(f"no installation with id {installation_id}")

    wanted = to_hash or row.replaced_hash
    if not wanted:
        raise ValueError(
            "that install replaced nothing, so there is no earlier version to go back to"
        )
    project = (
        session.query(ProjectModel).filter_by(id=row.project_id).one_or_none()
        if row.project_id
        else None
    )
    # force: the path currently holds what this install put there, which is
    # precisely the state a rollback exists to undo.
    return install(session, wanted, Path(row.target_path), row.agent, project, force=True)


def installations(session: Session, project_root: Path | None = None) -> list[dict[str, Any]]:
    """What flanner has installed where, newest first."""
    query = session.query(SkillInstallationModel)
    if project_root is not None:
        project = get_project_by_root(session, str(project_root))
        if project is None:
            return []
        query = query.filter(SkillInstallationModel.project_id == project.id)

    return [
        {
            "id": str(row.id),
            "agent": row.agent,
            "target": row.target_path,
            # The directory's own name, split here rather than in a template:
            # a Windows path separator is an escape character in Jinja.
            "name": Path(row.target_path).name,
            "manifest_hash": row.manifest_hash,
            "replaced_hash": row.replaced_hash or None,
            "status": row.status,
            "intact": row.status == INSTALLED and _still_matches(row),
            "installed_at": row.created_at.isoformat() + "Z",
        }
        for row in query.order_by(SkillInstallationModel.created_at.desc()).all()
    ]


def _still_matches(row: SkillInstallationModel) -> bool:
    """Whether the installed directory still holds what was installed."""
    target = Path(row.target_path)
    if not target.is_dir():
        return False
    digest, *_ = skills_ops.manifest_hash(target)
    return digest == row.observed_hash


def stored() -> list[dict[str, Any]]:
    """Every snapshot in the store, with whether it still verifies."""
    base = store_root()
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.iterdir()):
        if not path.is_dir() or ".partial-" in path.name:
            continue
        digest = f"sha256:{path.name}"
        actual, count, size, _cut = skills_ops.manifest_hash(path)
        out.append(
            {
                "manifest_hash": digest,
                "path": str(path),
                "files": count,
                "size_bytes": size,
                "verified": actual == digest,
            }
        )
    return out
