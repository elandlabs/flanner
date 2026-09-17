"""Manifest exchange and verified ingest (PRD §14).

Two peers reconcile by comparing manifests: each says which artifacts it
holds, and each pulls only what it lacks. Because artifacts are immutable and
content-addressed, that comparison is a set difference with no merge logic
and no notion of "newer" to get wrong.

Everything arriving from a peer is hostile until proven otherwise. An
artifact is verified *before* it is stored, never after, and a rejection is
recorded with its reason but never with its content (§14.4). Nothing here
raises on bad input: a peer must not be able to halt a sync by sending
garbage.

Transport lives elsewhere. A peer is anything answering :class:`Peer`, so
the same rules govern a sync between two sessions in one process and a sync
across a network: :mod:`flanner.peer` supplies the latter without changing
anything here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from . import artifacts
from .artifacts import Artifact
from .database import VersionModel, get_artifact, list_artifacts, save_envelope
from .frontmatter import read_managed

PROTOCOL_VERSION = 1

#: How a refusal for an author we hold no key for begins. A constant because
#: the push path has to tell this failure apart from every other one — it is
#: the only kind a keyring refresh could fix — and matching on a sentence
#: someone might later reword is not a way to decide that.
UNKNOWN_AUTHOR = "no known key for device"


def is_unknown_author(verdict: Any) -> bool:
    """Whether this refusal might be fixed by learning a new device key."""
    return not verdict and str(verdict.reason or "").startswith(UNKNOWN_AUTHOR)


# A single artifact payload is bounded before anything is allocated for it,
# so an oversized or decompression-bomb response is refused rather than
# absorbed (PRD §14.4).
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

# How many artifacts one fetch may ask for. A first sync can want thousands,
# and a single request for all of them is both a large allocation on the
# serving side and an all-or-nothing failure on ours.
MAX_FETCH_BATCH = 100

# Push is the one operation where a peer hands us data we did not ask for,
# so it is capped harder than fetch in both directions. A fetch batch is
# bounded because *we* chose to want that much; a push batch is bounded
# because somebody else did.
MAX_PUSH_BATCH = 50

# Total payload bytes in one push. `MAX_PAYLOAD_BYTES` already bounds any
# single artifact; this bounds the request, so fifty maximum-size artifacts
# cannot arrive as one 400 MB allocation.
MAX_PUSH_BYTES = 16 * 1024 * 1024

# The transport's own ceiling, checked before a byte is parsed. Every limit
# above is enforced after the body has been read and turned into a dict, so
# on their own they bound what a peer may *store* and not what it may make
# this device allocate. Artifacts travel base64-encoded inside json, which
# costs about a third on top of `MAX_PUSH_BYTES`; the rest is headroom for
# the envelopes around them.
MAX_REQUEST_BYTES = 32 * 1024 * 1024

# Pushes one device may make in `PUSH_WINDOW` seconds. Authorised is not the
# same as careful: a colleague running a script in a loop must not be able
# to fill your disk, and the limit is per device so one misbehaving laptop
# cannot lock out the rest of the team.
PUSH_WINDOW = 60.0
MAX_PUSHES_PER_WINDOW = 30

# How long to wait between keyring refreshes, across all peers. A push from
# an author we do not know is the one refusal a refresh could fix, but the
# party asking is remote: without a floor, fifty artifacts signed by fifty
# invented device ids would be fifty calls to the control plane. One refresh
# serves every unknown author at once, because the keyring is per
# organisation and not per device.
KEYRING_COOLDOWN = 300.0


@dataclass(frozen=True)
class Manifest:
    """What one peer holds for a workspace.

    Sent only to an already-authorized peer, and never to Flanner Mesh or
    the network provider: the id set alone would disclose the shape of a
    team's planning activity (PRD §14.3).
    """

    workspace_id: str
    artifact_ids: frozenset[str] = frozenset()
    heads: frozenset[str] = frozenset()
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "workspace_id": self.workspace_id,
            "artifact_ids": sorted(self.artifact_ids),
            "heads": sorted(self.heads),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        try:
            return cls(
                workspace_id=str(data["workspace_id"]),
                artifact_ids=frozenset(data.get("artifact_ids") or ()),
                heads=frozenset(data.get("heads") or ()),
                protocol_version=int(data.get("protocol_version", PROTOCOL_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Malformed manifest: {e}") from None


def build_manifest(
    session: Session, workspace_id: str, *, hidden: frozenset[str] | set[str] = frozenset()
) -> Manifest:
    """Describe what this device holds for a workspace.

    ``hidden`` names plans claimed as retired. Their artifacts are left out
    of a manifest *sent to a peer*, so the peer never asks for something it
    would only be refused — and refusing after offering would make every
    sync re-request the same ids forever.

    The tombstones themselves are never hidden. They are how a peer learns
    the claim exists at all, and withholding them would mean the retirement
    stopped at this device.

    Never pass ``hidden`` when describing our own catalog to ourselves: the
    result feeds "what am I missing", and hiding our own artifacts there
    would have us fetch them back from a peer.
    """
    held = [
        a
        for a in list_artifacts(session)
        if a.workspace_id == workspace_id
        and not (
            hidden and a.plan_file_id in hidden and a.artifact_type != artifacts.PLAN_TOMBSTONE
        )
    ]
    graph: dict[str, tuple[str, ...]] = {}
    for artifact in held:
        graph[artifact.artifact_id] = _parents_of(artifact)
    return Manifest(
        workspace_id=workspace_id,
        artifact_ids=frozenset(graph),
        heads=frozenset(artifacts.find_heads(graph)),
    )


def _parents_of(row: Any) -> tuple[str, ...]:
    import json

    try:
        return tuple(json.loads(row.parents))
    except (ValueError, TypeError):
        return ()


#: Artifact types that carry a memory rather than a plan. Named here
#: because both the digest rule and the ingest path need the same list, and
#: two lists is how they come to disagree.
MEMORY_TYPES = frozenset({artifacts.MEMORY_RECORD, artifacts.MEMORY_TOMBSTONE})

#: Artifact types carrying a skill package. Separate from the memory list
#: because they mean different things on arrival: a memory is materialised
#: and can be recalled straight away, while a package is only filed as a
#: transfer and waits for somebody here to install it.
SKILL_TYPES = frozenset({artifacts.SKILL_PACKAGE})


def payload_digest(artifact_type: str, payload: bytes) -> str:
    """The hash an envelope's ``content_hash`` must equal for this payload.

    A plan version travels as its whole managed file, because the receiver
    needs the plan name and version number to materialize it, while the
    signed hash covers the body alone (PRD §12.4). Every other artifact
    type is hashed exactly as delivered.
    """
    if artifact_type == artifacts.PLAN_VERSION:
        _, body = read_managed(payload.decode("utf-8", errors="replace"))
        return artifacts.hash_text(body)
    return artifacts.hash_bytes(payload)


def missing_artifact_ids(local: Manifest, remote: Manifest) -> set[str]:
    """What the remote holds that we do not.

    A plain set difference: immutability means an id we already hold can
    never have different content, so there is nothing to compare or merge.
    """
    if local.workspace_id != remote.workspace_id:
        return set()
    return set(remote.artifact_ids) - set(local.artifact_ids)


class Peer(Protocol):
    """The other side of a sync. Any transport that can answer these."""

    def manifest(self, workspace_id: str) -> Manifest: ...

    def fetch(self, artifact_ids: list[str]) -> list[tuple[dict[str, Any], bytes | None]]: ...


# Resolves a device id to its public key. In a team this is backed by the
# control plane's device registry (PRD §11.2); locally it is just this device.
KeyResolver = Any


@dataclass
class SyncReport:
    """What one sync accomplished, and what it refused."""

    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    already_held: list[str] = field(default_factory=list)

    #: Accepted and stored, but no file was written. A separate list from
    #: `rejected` on purpose: the artifact verified and is held, so the sync
    #: succeeded — what failed is only the local convenience of a file, and
    #: reporting it as a rejection would understate what was received.
    unreadable: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


def ingest_artifact(
    session: Session,
    envelope: dict[str, Any],
    payload: bytes | None,
    resolve_key: KeyResolver,
) -> artifacts.Verdict:
    """Verify a foreign artifact, then store it. Never the other way round.

    Returns a Verdict rather than raising, so one hostile artifact cannot
    abort a sync that also carries good ones.
    """
    try:
        artifact = Artifact.from_dict(envelope)
    except ValueError as e:
        return artifacts.Verdict(False, str(e))

    if payload is not None and len(payload) > MAX_PAYLOAD_BYTES:
        return artifacts.Verdict(False, f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")

    public_key = resolve_key(artifact.actor_device_id)
    if not public_key:
        # Two very different situations, and we cannot tell them apart from
        # here: a revoked or forged device, or a new teammate whose key this
        # device has not learned yet. Both are refused, because guessing
        # wrong in the permissive direction is the expensive mistake. The
        # message names the recoverable one so somebody reading a refusal
        # knows there is something to try.
        return artifacts.Verdict(
            False,
            f"{UNKNOWN_AUTHOR} {artifact.actor_device_id}"
            " (sign in again if this is a new teammate)",
        )

    verdict = artifacts.verify_artifact(artifact, public_key)
    if not verdict:
        return verdict
    if payload is not None and payload_digest(artifact.artifact_type, payload) != (
        artifact.content_hash
    ):
        return artifacts.Verdict(False, "payload does not match content_hash")

    body = payload.decode("utf-8", errors="replace") if payload is not None else None
    save_envelope(session, artifact, payload=body)
    if artifact.artifact_type in MEMORY_TYPES:
        # Here rather than in the pull loop, because push goes through this
        # function too and a memory that arrived by push would otherwise be
        # a row nobody ever sees. Stored either way: `materialise` can
        # decline the content — a peer sending a credential, say — and an
        # artifact that verified is still a verified artifact.
        from . import memory_ops

        memory_ops.materialise(
            session, envelope=artifact, body=body or "", workspace_id=artifact.workspace_id
        )
    elif artifact.artifact_type in SKILL_TYPES and payload is not None:
        # Filed, not installed. The signature and the content hash are
        # already checked above; what this does is record that a package
        # arrived and whether it hashes to what its sender claimed, so
        # somebody here can decide about it.
        from . import skills_mesh

        skills_mesh.materialise(
            session, envelope=artifact, payload=payload, workspace_id=artifact.workspace_id
        )
    return artifacts.Verdict(True)


def sync_from_peer(
    session: Session,
    peer: Peer,
    workspace_id: str,
    resolve_key: KeyResolver,
    *,
    project: Any = None,
) -> SyncReport:
    """Pull everything a peer holds for a workspace that this device lacks.

    Idempotent: running it again with no new artifacts on either side does
    nothing, and re-receiving an artifact already held is recorded rather
    than treated as a conflict.
    """
    report = SyncReport()
    try:
        remote = peer.manifest(workspace_id)
    except Exception as e:  # a peer that cannot answer is not a crash
        report.rejected.append(("<manifest>", f"peer manifest unavailable: {e}"))
        return report

    if remote.protocol_version != PROTOCOL_VERSION:
        report.rejected.append(
            ("<manifest>", f"unsupported peer protocol {remote.protocol_version}")
        )
        return report
    if remote.workspace_id != workspace_id:
        report.rejected.append(("<manifest>", "peer answered for a different workspace"))
        return report

    wanted = sorted(missing_artifact_ids(build_manifest(session, workspace_id), remote))
    # "Nothing new" used to return here, before the pass at the bottom — so
    # the one situation that pass exists for, a second pull after a first
    # that stored a plan it could not write, was the one situation in which
    # it never ran.
    delivered: list[tuple[dict[str, Any], bytes | None]] = []
    if wanted:
        try:
            delivered = peer.fetch(wanted)
        except Exception as e:
            report.rejected.append(("<fetch>", f"peer fetch failed: {e}"))
            return report

    asked_for = set(wanted)
    for envelope, payload in delivered:
        artifact_id = str(envelope.get("artifact_id", "<unknown>"))
        # A peer must not smuggle in artifacts that were never requested.
        if artifact_id not in asked_for:
            report.rejected.append((artifact_id, "peer sent an artifact that was not requested"))
            continue
        if get_artifact(session, artifact_id) is not None:
            report.already_held.append(artifact_id)
            continue
        # The envelope must belong to the workspace being synchronized.
        if envelope.get("workspace_id") != workspace_id:
            report.rejected.append((artifact_id, "artifact belongs to a different workspace"))
            continue
        verdict = ingest_artifact(session, envelope, payload, resolve_key)
        if not verdict:
            report.rejected.append((artifact_id, verdict.reason))
            continue
        report.accepted.append(artifact_id)
        make_readable(session, envelope, payload, report, preferred=project)

    _write_what_is_held(session, workspace_id, report, preferred=project)
    return report


def _write_what_is_held(
    session: Session, workspace_id: str, report: SyncReport, *, preferred: Any
) -> None:
    """Give stored plan versions that never became files another chance.

    The fetch loop only ingests what this device lacks, so an artifact that
    was stored and could not be written — no project in the workspace yet,
    two projects and no way to choose, a disk that said no — would never be
    looked at again: every later pull reports it as already held and moves
    on. "The file can be written on the next attempt" is only true if the
    next attempt tries, so this is the trying.

    It is also what makes refusing to guess acceptable. A pull that reports
    "two projects share that workspace" is followed by one with --project,
    and this pass is where that second pull does the writing.

    Only artifacts with no version row. This device's own plans always have
    one — creating, updating and adopting all record the artifact id — so
    they are never re-materialised here, which matters: written again they
    would land as conflict copies of themselves.
    """
    held = [
        row.artifact_id
        for row in list_artifacts(session, artifact_type=artifacts.PLAN_VERSION)
        if row.workspace_id == workspace_id
    ]
    if not held:
        return
    written = {
        row.artifact_id
        for row in session.query(VersionModel).filter(VersionModel.artifact_id.in_(held))
    }
    # Not the ones this very pull already tried: those were either written
    # or reported a moment ago, and reporting one twice reads as two plans.
    tried = set(report.accepted) | {artifact_id for artifact_id, _ in report.unreadable}
    pending = [a for a in held if a not in written and a not in tried]
    for envelope, payload in LocalPeer(session).fetch(pending):
        make_readable(session, envelope, payload, report, preferred=preferred)


def make_readable(
    session: Session,
    envelope: dict[str, Any],
    payload: bytes | None,
    report: SyncReport,
    *,
    preferred: Any = None,
) -> None:
    """Turn an accepted plan version into a file somebody can open.

    Without this, "accepted" meant a row in a table. The artifact verified
    and was stored, and then nothing wrote a file, created a version record,
    or told the catalog it existed — so `flanner history` could not find it,
    the web UI could not show it, and an agent could not read or revise it.
    The plan a teammate sent was, for every practical purpose, not there.

    Only plan versions. Comments, decisions and tombstones are events that
    the review surface reads straight from the artifact store; they have no
    file and want none.

    Failure is reported, never raised. The artifact is verified and stored by
    the time this runs, so a disk that refused the write must not un-accept
    something cryptographically valid — the sync stays successful and the
    file can be written on the next attempt.
    """
    if envelope.get("artifact_type") != artifacts.PLAN_VERSION:
        return
    if payload is None:
        # Reported, not skipped. A silent return here is a plan that was
        # accepted and then vanished, which is the shape of the original
        # defect all over again.
        report.unreadable.append(
            (str(envelope.get("artifact_id", "?")), "the artifact arrived without its content")
        )
        return

    project, why = _home_for(session, envelope, preferred)
    if project is None:
        report.unreadable.append((str(envelope.get("artifact_id", "?")), why))
        return

    from . import plan_ops

    result = plan_ops.materialize_version(
        session, project=project, envelope=envelope, managed_file=payload
    )
    if result.version is None and result.reason:
        report.unreadable.append((str(envelope.get("artifact_id", "?")), result.reason))


def _home_for(session: Session, envelope: dict[str, Any], preferred: Any) -> tuple[Any, str]:
    """Which local project a pulled plan belongs in, or why none does.

    A workspace is a team, and a team has more than one repository, so the
    workspace id alone cannot say where a plan goes. This used to be
    `.first()`: whichever project the query happened to return, which is
    not a rule anyone could predict and put plans in the wrong checkout.

    In order:

    1. A plan this device already holds goes where it lives. Same plan id,
       same project; nothing to decide.
    2. The project the person named — `peer pull --project`, or the one
       they ran it from — is used, if it is in the workspace.
    3. A workspace with exactly one local project needs no choosing.

    Anything else is reported rather than guessed. The artifact stays
    stored and verified, and the next pull with `--project` writes it.
    """
    from uuid import UUID

    from .database import PlanFileModel, ProjectModel

    workspace_id = str(envelope.get("workspace_id") or "")
    if not workspace_id:
        return None, "the artifact names no workspace"

    plan_id = str(envelope.get("plan_file_id") or "")
    if plan_id:
        try:
            held = session.get(PlanFileModel, UUID(plan_id))
        except ValueError:
            held = None
        if held is not None and held.project.workspace_id == workspace_id:
            return held.project, ""

    if preferred is not None and preferred.workspace_id == workspace_id:
        return preferred, ""

    candidates = session.query(ProjectModel).filter_by(workspace_id=workspace_id).all()
    if len(candidates) == 1:
        return candidates[0], ""
    if not candidates:
        return None, "no local project is in that workspace"
    names = ", ".join(sorted(candidate.name for candidate in candidates))
    return None, (
        f"{len(candidates)} local projects share that workspace ({names}); "
        "pull with --project to say which"
    )


class LocalPeer:
    """A peer backed by another local session.

    Used by the tests to exercise the whole protocol without a network, and
    useful in its own right for reconciling two catalogs on one machine.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def manifest(self, workspace_id: str) -> Manifest:
        return build_manifest(self._session, workspace_id)

    def _payload_for(self, row: Any) -> bytes | None:
        """The bytes to send for an artifact.

        Event payloads are stored on the row. A plan version's content is the
        managed file itself, which stays the canonical copy rather than being
        duplicated into the catalog (PRD §12.4).
        """
        if row.artifact_type == artifacts.PLAN_VERSION:
            version = (
                self._session.query(VersionModel).filter_by(artifact_id=row.artifact_id).first()
            )
            if version is not None:
                try:
                    return Path(version.file_path).read_bytes()
                except OSError:
                    pass
            # No file yet, or none readable: a version that arrived from a
            # peer and has not been written to a project. Ingest keeps its
            # bytes on the row for exactly this case. Answering None here
            # made such a plan unwritable by any later pull and unrelayable
            # to a third device — stored, verified, and stuck.
        return row.payload.encode("utf-8") if row.payload is not None else None

    def fetch(self, artifact_ids: list[str]) -> list[tuple[dict[str, Any], bytes | None]]:
        out: list[tuple[dict[str, Any], bytes | None]] = []
        for artifact_id in artifact_ids:
            row = get_artifact(self._session, artifact_id)
            if row is None:
                continue
            envelope = {
                "protocol_version": row.protocol_version,
                "artifact_id": row.artifact_id,
                "artifact_type": row.artifact_type,
                "organization_id": row.organization_id,
                "workspace_id": row.workspace_id,
                "plan_file_id": row.plan_file_id,
                "parents": list(_parents_of(row)),
                "created_at": row.created_at,
                "actor_user_id": row.actor_user_id,
                "actor_device_id": row.actor_device_id,
                "content_hash": row.content_hash,
                "signature": row.signature,
            }
            # Only when set, exactly as `Artifact.to_dict` writes it: the id
            # covers this field, so leaving it out made every shared memory
            # fail the receiver's "id does not match its envelope" check.
            if row.memory_id:
                envelope["memory_id"] = row.memory_id
            out.append((envelope, self._payload_for(row)))
        return out
