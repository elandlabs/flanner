"""Shared plan write-path.

`write_version` is the one place that turns a body of markdown into a stored
plan version: frontmatter, filename, save to disk, hash, version record. The
MCP server and the web UI both call it so the sequence cannot drift between
surfaces.

`record_new_version` is the locked update path: it refreshes the plan row,
picks the next free version number (skipping any number whose file already
exists on disk), writes the version, bumps ``current_version``, and commits,
all under a cross-process lock. Two MCP clients, or the MCP server and the
web UI, can therefore never write the same version number or overwrite each
other's files (PRD Phase 1: single-writer discipline).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from . import artifacts
from .database import (
    ArtifactModel,
    PlanFileModel,
    ProjectModel,
    VersionModel,
    create_version,
    get_artifact,
    get_plan_file,
    get_version,
    list_versions,
    save_envelope,
)
from .database import create_plan_file as db_create_plan_file
from .exceptions import DatabaseError
from .frontmatter import create_plan_file_content, generate_frontmatter, read_managed
from .storage import exclusive_lock, save_plan_file_with_frontmatter
from .utils import generate_file_name, hash_content, utcnow

# One file-based lock per plan, so it works across unrelated processes on
# every OS. Was one lock per directory, which made two writers on different
# plans in the same project wait for each other for no reason.


def workspace_id_for(project: ProjectModel) -> str:
    """The workspace this project's artifacts belong to.

    Real workspace ids are opaque and issued by the control plane. Until a
    project joins one, artifacts still need a stable workspace to belong to,
    so a local id is derived from the project. It never leaves the machine,
    and no entitlement can ever grant a role in it, which is exactly why
    authorization stays advisory while a project is solo.
    """
    return project.workspace_id or f"local:{project.id}"


def _parent_artifact_ids(session: Session, plan_file: PlanFileModel) -> tuple[str, ...]:
    """The artifact this new version descends from, if there is one.

    Lineage is per plan, so a new version's parent is the latest signed
    version of the same plan. Versions written before artifacts existed have
    no id, which correctly yields a root rather than a broken link.
    """
    latest = get_version(session, plan_file.id, None)
    if latest is None or not latest.artifact_id:
        return ()
    return (latest.artifact_id,)


def plan_write_lock(project_root: str, plan_directory: str, plan_id: Any) -> Any:
    """Cross-process write lock for one plan.

    Every holder acts on a single named plan, so the directory was never the
    thing needing protection. Name assignment is not covered here and does
    not need to be: uniqueness is a database constraint, and the plan row
    exists before any of these locks is taken.

    Keyed by id rather than name. Names are user-supplied and would need
    sanitising to be a filename, and a rename would move a plan's lock out
    from under a live holder.

    The mechanism is `storage.exclusive_lock`; this names what a plan lock
    is and where it lives. The cost of keying per plan is that a crash
    strands one small file per plan rather than one per project, each
    cleared by the next writer of that plan. Nothing scans for them:
    `reconcile` globs `*.md` and the write guard only inspects `.md`.
    """
    return exclusive_lock(Path(project_root) / plan_directory, str(plan_id))


def create_plan(
    session: Session,
    *,
    project: ProjectModel,
    name: str,
    content: str,
    description: str = "",
    created_by: str,
    auto_version: bool = True,
) -> tuple[PlanFileModel, VersionModel]:
    """Create a plan record and write its initial version (v1).

    The one create sequence shared by every surface. Callers keep their own
    name validation and duplicate-error shaping; ``DuplicateError`` and
    ``NotFoundError`` from the database layer propagate.
    """
    plan_file = db_create_plan_file(
        session,
        project_id=project.id,
        name=name,
        description=description,
        auto_version=auto_version,
    )
    try:
        version = write_version(
            session,
            project=project,
            plan_file=plan_file,
            version=1,
            content=content,
            created_by=created_by,
            notes="Initial version",
        )
    except Exception:
        _undo_plan_row(session, plan_file.id)
        raise
    return plan_file, version


def _undo_plan_row(session: Session, plan_file_id: UUID) -> None:
    """Remove the plan record when its first version could not be written.

    The row is committed before the file is written, so a write that fails —
    a full disk, a read-only checkout, a directory standing where the file
    should go — leaves a plan with no versions. Nothing displays it usefully
    and, worse, it holds the name: every retry from then on is refused as a
    duplicate, so the failure is permanent and the only fix is deleting a
    row by hand. Reproduced directly, the second attempt raises
    DuplicateError even after the obstruction is gone.

    A cleanup that itself fails is swallowed. The write error is the one the
    caller needs; replacing it with a delete error would hide what actually
    went wrong, and the ghost row is no worse than it was.
    """
    session.rollback()
    with contextlib.suppress(Exception):
        stranded = session.get(PlanFileModel, plan_file_id)
        if stranded is not None:
            session.delete(stranded)
            session.commit()


def record_new_version(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    content: str,
    created_by: str,
    notes: str,
) -> VersionModel:
    """Write the next version of an existing plan and commit, under the lock.

    Refreshes ``plan_file`` after acquiring the lock so a concurrent writer's
    bump is seen, and never reuses a version number whose file already exists
    on disk (catalog drift heals forward instead of overwriting history).
    """
    root = project.project_root
    if root is None:
        raise DatabaseError(f"Project '{project.name}' has no project_root configured")

    with plan_write_lock(root, project.plan_directory, plan_file.id):
        session.refresh(plan_file)
        next_version = plan_file.current_version + 1
        plan_dir = Path(root) / project.plan_directory
        while (plan_dir / generate_file_name(plan_file.name, next_version)).exists():
            next_version += 1
        version = _write_version_unlocked(
            session,
            project=project,
            plan_file=plan_file,
            version=next_version,
            content=content,
            created_by=created_by,
            notes=notes,
        )
        plan_file.current_version = next_version
        plan_file.updated_at = utcnow()
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
    return version


def write_version(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    version: int,
    content: str,
    created_by: str,
    notes: str,
) -> VersionModel:
    """Write one version of a plan to disk and record it; return the version row.

    Takes the plan write lock, then delegates. Used by the create flows where
    the version number is fixed (v1); update flows use ``record_new_version``.
    Does not bump ``plan_file.current_version`` or commit; the caller owns
    that so it can update related state in one transaction.
    """
    root = project.project_root
    if root is None:
        # Callers guard this and return a friendly message; belt-and-suspenders.
        raise DatabaseError(f"Project '{project.name}' has no project_root configured")
    with plan_write_lock(root, project.plan_directory, plan_file.id):
        return _write_version_unlocked(
            session,
            project=project,
            plan_file=plan_file,
            version=version,
            content=content,
            created_by=created_by,
            notes=notes,
        )


def _write_version_unlocked(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    version: int,
    content: str,
    created_by: str,
    notes: str,
) -> VersionModel:
    """The raw write sequence; callers must hold the plan write lock."""
    root = project.project_root
    if root is None:
        raise DatabaseError(f"Project '{project.name}' has no project_root configured")

    # Normalize line endings before hashing and saving so the stored hash
    # matches the LF content that is written and later read back (CRLF from web
    # form submissions would otherwise make an unchanged plan look modified).
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    # Sign the body before writing. The artifact's content hash covers the
    # body only, never the generated frontmatter (PRD §12.4), which is what
    # lets the resulting artifact id be written *into* that frontmatter
    # without changing what was signed.
    artifact = artifacts.make_artifact(
        artifact_type=artifacts.PLAN_VERSION,
        workspace_id=workspace_id_for(project),
        content_hash=artifacts.hash_text(content),
        plan_file_id=str(plan_file.id),
        parents=_parent_artifact_ids(session, plan_file),
        actor_user_id=created_by,
    )

    frontmatter_str = generate_frontmatter(
        project_id=project.id,
        project_name=project.name,
        plan_file_id=plan_file.id,
        plan_name=plan_file.name,
        version=version,
        created_by=created_by,
        created_at=utcnow(),
        artifact_id=artifact.artifact_id,
        parents=artifact.parents,
        workspace_id=artifact.workspace_id,
        actor_device_id=artifact.actor_device_id,
    )
    full_content = create_plan_file_content(frontmatter_str, content)
    file_name = generate_file_name(plan_file.name, version)
    file_path = save_plan_file_with_frontmatter(
        project_root=root,
        plan_directory=project.plan_directory,
        file_name=file_name,
        content=full_content,
    )
    save_envelope(session, artifact)
    # The artifact id goes in with the INSERT. Assigning it afterwards would
    # leave it unflushed behind create_version's own commit, and a caller that
    # never commits (the create path does not) would silently lose the link,
    # breaking the parent chain for the next version.
    return create_version(
        session,
        plan_file_id=plan_file.id,
        version=version,
        file_path=file_path,
        content_hash=hash_content(content),  # hash the body, not the frontmatter
        created_by=created_by,
        notes=notes,
        artifact_id=artifact.artifact_id,
    )


@dataclass(frozen=True)
class MaterializeResult:
    """Outcome of turning a synced artifact into a working file."""

    version: VersionModel | None = None
    reason: str = ""
    conflict_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.version is not None


def _conflict_name(plan_name: str, version: int, artifact_id: str) -> str:
    """A deterministic filename for a version number two devices both used.

    PRD §12.4 requires both artifacts to be retained until someone merges
    them, so the loser of a filename race is parked under a name derived
    from its own artifact id: stable, collision-free, and obviously a
    conflict to anyone looking at the directory.
    """
    digest = artifact_id.split(":", 1)[-1][:12]
    return generate_file_name(f"{plan_name}__conflict-{digest}", version)


class _Unusable(Exception):
    """Why an incoming plan file cannot be written here.

    An exception rather than five early returns: each check answers the same
    question — is this file usable — and the caller does the same thing with
    every answer. Collapsing them puts the refusal in one place instead of
    interleaving it with the work.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Incoming:
    """A peer's plan version, parsed and checked, before anything is written.

    A record rather than a tuple because five values travel together through
    every step of materializing, and threading them as separate arguments is
    what pushed the helpers past a readable signature.
    """

    fm_data: dict[str, Any]
    body: str
    plan_name: str
    version: int
    plan_uuid: UUID
    artifact_id: str

    @property
    def author(self) -> str:
        """Who wrote the version, defaulting to the fact that a peer sent it."""
        return str(self.fm_data.get("created_by") or "peer")


def _read_incoming(envelope: dict[str, Any], managed_file: bytes) -> _Incoming:
    """Parse and check a peer's plan file before anything is written.

    The content-hash check is the load-bearing one: the envelope was signed
    over the hash, so a body that does not match it is either corrupt or
    substituted, and either way must not reach the working tree.

    Raises:
        _Unusable: with a reason the caller can report verbatim.
    """
    try:
        fm_data, body = read_managed(managed_file.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _Unusable(f"unreadable plan file: {e}") from None

    if artifacts.hash_text(body) != envelope.get("content_hash"):
        raise _Unusable("body does not match the signed content hash")

    plan_name = str(fm_data.get("plan_name") or "")
    raw_version = fm_data.get("version")
    if not plan_name or not isinstance(raw_version, int):
        raise _Unusable("plan file is missing its name or version")

    try:
        plan_uuid = UUID(str(fm_data.get("plan_file_id")))
    except (ValueError, TypeError):
        raise _Unusable("plan file has no usable plan_file_id") from None

    return _Incoming(
        fm_data=fm_data,
        body=body,
        plan_name=plan_name,
        version=raw_version,
        plan_uuid=plan_uuid,
        artifact_id=str(envelope.get("artifact_id", "")),
    )


def _plan_for_incoming(
    session: Session,
    *,
    project: ProjectModel,
    plan_uuid: UUID,
    plan_name: str,
    description: str,
) -> PlanFileModel:
    """Find the plan this version belongs to, creating it if it is new here.

    Identity is the uuid, never the name. Two plans can share a human name
    and be unrelated, so a name collision on a different id is refused rather
    than merged — fusing two histories is not recoverable, and refusing is.

    Raises:
        _Unusable: when the name collides, or the plan is another project's.
    """
    plan_file = get_plan_file(session, plan_uuid)
    if plan_file is not None:
        if plan_file.project_id != project.id:
            raise _Unusable("plan belongs to a different project on this device")
        return plan_file

    clash = session.query(PlanFileModel).filter_by(project_id=project.id, name=plan_name).first()
    if clash is not None:
        raise _Unusable(f"a different plan named '{plan_name}' already exists here")

    return db_create_plan_file(
        session,
        project_id=project.id,
        name=plan_name,
        description=description,
        plan_file_id=plan_uuid,
    )


def _frontmatter_for(
    project: ProjectModel,
    plan_file: PlanFileModel,
    incoming: _Incoming,
    envelope: dict[str, Any],
) -> str:
    """Rebuild the header for this device rather than copying the peer's.

    The project ids are ours; the artifact identity, name, version, author
    and timestamp belong to the version and are carried over exactly.
    """
    return generate_frontmatter(
        project_id=project.id,
        project_name=project.name,
        plan_file_id=plan_file.id,
        plan_name=incoming.plan_name,
        version=incoming.version,
        created_by=incoming.author,
        created_at=_parse_stamp(incoming.fm_data.get("created_at")),
        artifact_id=incoming.artifact_id,
        parents=envelope.get("parents") or None,
        workspace_id=str(envelope.get("workspace_id") or ""),
        actor_device_id=str(envelope.get("actor_device_id") or ""),
    )


def _target_name(plan_dir: Path, incoming: _Incoming) -> tuple[str, str | None]:
    """Where this version goes, and whether it had to go somewhere else.

    Never overwrites. A file already sitting on this version number is either
    this same artifact or a concurrent one somebody wrote, and both are worth
    keeping, so a clash lands beside it under a conflict name.

    Returns the file name and the conflict path, or None if there was no clash.
    """
    file_name = generate_file_name(incoming.plan_name, incoming.version)
    target = plan_dir / file_name
    if not target.exists():
        return file_name, None

    here = artifacts.hash_text(read_managed(target.read_text("utf-8"))[1])
    if here == artifacts.hash_text(incoming.body):
        return file_name, None

    file_name = _conflict_name(incoming.plan_name, incoming.version, incoming.artifact_id)
    return file_name, str(plan_dir / file_name)


def _authored_here(session: Session, plan_file: PlanFileModel) -> bool:
    """Whether this device has ever written a version of this plan itself.

    The question that decides whether an incoming version may move the
    current-version pointer. If you have authored anything here, the pointer
    is a choice you made and a teammate does not get to change it by
    pushing; if every version you hold arrived from somebody else, there is
    nothing of yours to displace and following along is what you want.

    A version with **no artifact id** predates signing, which means it was
    written locally, so it counts as authored here. Treating it as foreign
    would let a peer move the pointer on the one class of plan that has
    never left this machine.

    A version whose artifact id we cannot resolve does *not* count. That is
    a version record pointing at an artifact this store does not hold, which
    the append-only store makes vanishingly rare — and reading it as "mine"
    froze the pointer on a device that had only ever received, leaving a
    whole history stuck on its oldest version.
    """
    from . import identity

    mine = identity.device_id()
    return any(
        _written_here(session, version, mine) for version in list_versions(session, plan_file.id)
    )


def _written_here(session: Session, version: VersionModel, mine: str) -> bool:
    """Whether one version row was authored by this device."""
    if not version.artifact_id:
        return True
    stored = get_artifact(session, version.artifact_id)
    return stored is not None and stored.actor_device_id == mine


@dataclass(frozen=True)
class Standing:
    """Who started a plan, and what has arrived that you have not taken.

    Both halves are things `flanner list` could not say. Ownership was
    invisible, so on a shared plan nobody could tell whose it was; and a
    peer's newer version sat in the catalog with no indication it was there,
    because the current-version pointer deliberately does not move for it.
    A notification you have to run `history` to discover is not one.
    """

    owner: str
    waiting: int | None

    @property
    def has_incoming(self) -> bool:
        return self.waiting is not None


def standing(session: Session, plan_file: PlanFileModel) -> Standing:
    """The owner and the highest version that arrived and was not taken.

    Two shapes count as waiting, and the second is the common one. A peer
    can be *ahead* of you, which is a version numbered above the pointer;
    or you can both have written the same version number, which lands as a
    conflict file beside yours and shares its number. Only comparing
    against the pointer would miss every conflict, which is precisely the
    case somebody needs to be told about.
    """
    from . import identity

    mine = identity.device_id()
    current = plan_file.current_version or 0
    owner = ""
    foreign: list[int] = []
    hold_the_current = False
    for version in list_versions(session, plan_file.id):
        if version.version == 1 and not owner:
            owner = version.created_by or ""
        if _written_here(session, version, mine):
            hold_the_current = hold_the_current or version.version == current
        else:
            foreign.append(version.version)

    ahead = [v for v in foreign if v > current]
    if ahead:
        waiting: int | None = max(ahead)
    elif hold_the_current and current in foreign:
        waiting = current
    else:
        waiting = None
    return Standing(owner=owner or "user", waiting=waiting)


def materialize_version(
    session: Session,
    *,
    project: ProjectModel,
    envelope: dict[str, Any],
    managed_file: bytes,
) -> MaterializeResult:
    """Write a peer's verified plan version into this device's working tree.

    The caller has already verified the envelope and payload; this turns an
    accepted artifact into something a human and an agent can actually open.

    Frontmatter is regenerated rather than copied, because the project ids
    and name are this device's, while the artifact identity, plan name,
    version number, author, and timestamp all belong to the version and are
    preserved exactly. The body is written byte for byte, so the signed
    content hash still verifies here.
    """
    root = project.project_root
    if root is None:
        return MaterializeResult(reason=f"Project '{project.name}' has no project_root configured")

    try:
        incoming = _read_incoming(envelope, managed_file)
    except _Unusable as e:
        return MaterializeResult(reason=e.reason)

    with plan_write_lock(root, project.plan_directory, incoming.plan_uuid):
        existing = (
            session.query(VersionModel).filter_by(artifact_id=incoming.artifact_id).first()
            if incoming.artifact_id
            else None
        )
        if existing is not None:
            return MaterializeResult(version=existing, reason="already materialized")

        try:
            plan_file = _plan_for_incoming(
                session,
                project=project,
                plan_uuid=incoming.plan_uuid,
                plan_name=incoming.plan_name,
                description=str(incoming.fm_data.get("description") or ""),
            )
        except _Unusable as e:
            return MaterializeResult(reason=e.reason)

        plan_dir = Path(root) / project.plan_directory
        file_name, conflict_path = _target_name(plan_dir, incoming)
        file_path = save_plan_file_with_frontmatter(
            project_root=root,
            plan_directory=project.plan_directory,
            file_name=file_name,
            content=create_plan_file_content(
                _frontmatter_for(project, plan_file, incoming, envelope), incoming.body
            ),
        )
        version = create_version(
            session,
            plan_file_id=plan_file.id,
            version=incoming.version,
            file_path=file_path,
            content_hash=hash_content(incoming.body),
            created_by=incoming.author,
            notes=str(incoming.fm_data.get("notes") or ""),
            artifact_id=incoming.artifact_id or None,
        )
        # The pointer does **not** move because a teammate sent something.
        #
        # It used to, on "higher number and no conflict", and that is the one
        # way a peer could change what you see: the file was never
        # overwritten, but `flanner show`, the web UI and any agent all read
        # the current version, so somebody else's push silently changed what
        # you opened. Which version is current is a decision, and decisions
        # are made through review — `workflow.make_accepted_head` exists to
        # record exactly that, citing what justified it.
        #
        # The exception is a plan this device has only ever received. There
        # is no work of yours to displace, so the pointer tracks along —
        # which is what makes a whole history arriving in order end up
        # pointing at its newest version rather than its oldest.
        if not _authored_here(session, plan_file):
            plan_file.current_version = incoming.version
            plan_file.updated_at = utcnow()
        session.commit()

    return MaterializeResult(version=version, conflict_path=conflict_path)


def _parse_stamp(raw: Any) -> datetime | None:
    """The version's own timestamp, so materializing preserves when it was written."""
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Adoption:
    """What joining a workspace carried across, and what it left behind."""

    adopted: tuple[str, ...] = ()
    already_there: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def moved(self) -> int:
        return len(self.adopted)


def _resign_into(
    session: Session,
    plan_file: PlanFileModel,
    latest: VersionModel,
    workspace_id: str,
    content_hash: str,
) -> None:
    """Sign this plan's head afresh into a workspace, and point the row at it.

    `content_hash` is passed rather than read off `latest` so that "the head
    actually has a hash" is a precondition in the signature, checked by the
    caller that knows how to report a plan it cannot adopt.

    Signed with **no parents**: see `adopt_into_workspace` for why a lineage
    is not carried across a workspace boundary.

    Repointing the version row is the load-bearing half. A plan version's
    payload is reached through that row, so until it names the new artifact
    the content is not reachable to a peer, and the next version would chain
    back across the boundary rather than into the workspace.
    """
    artifact = artifacts.make_artifact(
        artifact_type=artifacts.PLAN_VERSION,
        workspace_id=workspace_id,
        content_hash=content_hash,
        plan_file_id=str(plan_file.id),
        parents=(),
        actor_user_id=latest.created_by or "user",
    )
    save_envelope(session, artifact)
    latest.artifact_id = artifact.artifact_id


def adopt_into_workspace(
    session: Session,
    *,
    project: ProjectModel,
    workspace_id: str,
) -> Adoption:
    """Re-root each plan's current content in a workspace it can sync from.

    A workspace id sits inside the signed envelope, so joining a team cannot
    move an artifact written before the join: re-signing it under the new
    workspace would produce a different id, and the signature that made the
    old one trustworthy would no longer describe anything. Without this, a
    team that adopts flanner after six months finds their existing plans
    invisible to each other, which reads as broken rather than as a design.

    So the current head of each plan is signed afresh into the workspace,
    with **no parents**. It is a root there, deliberately:

    - Its real parent lives in a workspace nobody else can verify. Pointing
      at it would hand every peer a link they can never resolve.
    - The history is not lost. Every local artifact stays exactly as it was,
      still verifiable, still on this machine. What does not travel is the
      record of *how* the plan reached its current state.

    That is the trade, and it is the honest one: teammates get the content
    they need today, and nobody is told a lineage crossed a boundary it
    did not. A plan with no signed version yet is skipped rather than
    invented.

    Idempotent. Adopting twice does nothing the second time, because the
    head is already in the workspace.
    """
    from .database import list_plan_files as _list_plan_files

    adopted: list[str] = []
    already: list[str] = []
    skipped: list[tuple[str, str]] = []

    for plan_file in _list_plan_files(session, project.id):
        latest = get_version(session, plan_file.id, None)
        if latest is None:
            skipped.append((plan_file.name, "no versions"))
            continue
        content_hash = latest.content_hash
        if not content_hash:
            skipped.append((plan_file.name, "no content hash"))
            continue

        current = (
            session.query(ArtifactModel).filter_by(artifact_id=latest.artifact_id).first()
            if latest.artifact_id
            else None
        )
        if current is not None and current.workspace_id == workspace_id:
            already.append(plan_file.name)
            continue

        _resign_into(session, plan_file, latest, workspace_id, content_hash)
        adopted.append(plan_file.name)

    session.commit()
    return Adoption(adopted=tuple(adopted), already_there=tuple(already), skipped=tuple(skipped))
