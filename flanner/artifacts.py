"""Immutable signed artifacts and their lineage (PRD §12).

An artifact is a typed, immutable payload plus a signed envelope. Identity is
cryptographic, not positional: the artifact id is a hash over the canonical
envelope, so two devices that independently produce the same content produce
the same id, and any alteration produces a different one.

Three separate checks make an artifact trustworthy, and they are deliberately
independent (PRD §12.2):

- the **artifact id** must equal the hash of the envelope it claims to describe
- the **signature** must be the actor device's, over those same bytes
- the **content hash** must match the payload actually delivered

Timestamps are descriptive. They never establish order: ordering comes from
the parent links alone, so a peer with a wrong clock, or a hostile one, cannot
reorder history (PRD §12.2, §12.3).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from . import identity

PROTOCOL_VERSION = 1

# Artifact types (PRD §12.1). Attachment chunks are named but not yet produced.
PLAN_VERSION = "plan.version"
PLAN_HEAD = "plan.head"
REVIEW_PROPOSAL = "review.proposal"
REVIEW_DECISION = "review.decision"
#: Notes from somebody with no device key, vouched for by the device that
#: imported them. A distinct type so nothing can mistake unverified,
#: outside review for a teammate's signed comment.
REVIEW_EXTERNAL = "review.external"
ACCEPTED_HEAD = "plan.accepted_head"
#: A signed request that peers stop showing and stop serving a plan. Not an
#: erasure: append-only means the bytes stay, and a device that was offline
#: when this was signed has them regardless. It is a claim other devices
#: honour, which is the strongest thing this design can honestly offer.
PLAN_TOMBSTONE = "plan.tombstone"
COMMENT = "comment"
FRESHNESS_EVIDENCE = "freshness.evidence"
ISSUE_LINK = "issue.link"
COLLAB_CHECKPOINT = "collaboration.checkpoint"
ATTACHMENT_MANIFEST = "attachment.manifest"

#: One memory, promoted to a workspace on purpose.
#:
#: A separate type from a plan version, not a flavour of one. A plan
#: is browsed and versioned; a memory is searched and corrected, and
#: a receiver that treated one as the other would file somebody's
#: durable context in a list nobody reads.
MEMORY_RECORD = "mem.record"

#: A signed request that peers stop recalling a memory.
#:
#: The same honesty as a plan tombstone: append-only means the bytes
#: stay, and a device that was offline when this was signed has them
#: regardless. It is a claim other devices honour, which is the
#: strongest thing this design can offer without pretending to reach
#: into somebody else's disk.
MEMORY_TOMBSTONE = "mem.tombstone"

#: A complete skill package, sent to a teammate on purpose.
#:
#: The bytes of the package and nothing else. No evidence, no recorded
#: uses, no session references: those are the private half of Flanner
#: Skills, and a package that carried them would make sharing a skill an
#: unannounced disclosure of how somebody works.
#:
#: Receiving one is not installing one. A verified package sits as a
#: transfer until somebody on the receiving machine says to install it,
#: which is the same rule an approval follows locally.
SKILL_PACKAGE = "skill.package"

ARTIFACT_TYPES = frozenset(
    {
        PLAN_VERSION,
        PLAN_HEAD,
        REVIEW_PROPOSAL,
        REVIEW_DECISION,
        REVIEW_EXTERNAL,
        ACCEPTED_HEAD,
        PLAN_TOMBSTONE,
        COMMENT,
        FRESHNESS_EVIDENCE,
        ISSUE_LINK,
        COLLAB_CHECKPOINT,
        ATTACHMENT_MANIFEST,
        MEMORY_RECORD,
        MEMORY_TOMBSTONE,
        SKILL_PACKAGE,
    }
)

# Envelope fields excluded from the identity hash: the id cannot cover itself,
# and the signature is made over the id's input.
_UNSIGNED_FIELDS = frozenset({"artifact_id", "signature"})


def hash_bytes(payload: bytes) -> str:
    """Content hash in the form used everywhere on the wire."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def hash_text(text: str) -> str:
    """Hash text as UTF-8, after normalizing line endings.

    Plan bodies cross platforms; without this a Windows checkout and a Linux
    one would disagree about the identity of identical content.
    """
    return hash_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def canonical_bytes(fields: dict[str, Any]) -> bytes:
    """Deterministic serialization of the envelope.

    Sorted keys and no incidental whitespace, so the same envelope hashes
    identically on any machine, in any Python version, forever. This encoding
    is part of the protocol: changing it changes every artifact id.
    """
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True)
class Artifact:
    """A signed, immutable envelope. Construct via :func:`make_artifact`."""

    artifact_type: str
    workspace_id: str
    content_hash: str
    actor_device_id: str
    created_at: str
    artifact_id: str = ""
    signature: str = ""
    protocol_version: int = PROTOCOL_VERSION
    organization_id: str | None = None
    plan_file_id: str | None = None
    #: Which memory this envelope is about, for the memory artifact types.
    #:
    #: Left out of the wire form when absent rather than sent as null.
    #: `to_dict` is exactly what the id and the signature cover, so a key
    #: present on every envelope would change the canonical bytes of every
    #: plan artifact ever signed, and every id derived from them. Omitting
    #: it leaves those bytes byte-for-byte as they were.
    memory_id: str | None = None
    actor_user_id: str | None = None
    parents: tuple[str, ...] = field(default_factory=tuple)

    def signing_fields(self) -> dict[str, Any]:
        """The envelope subset that the id and signature both cover."""
        return {k: v for k, v in self.to_dict().items() if k not in _UNSIGNED_FIELDS}

    def to_dict(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "organization_id": self.organization_id,
            "workspace_id": self.workspace_id,
            "plan_file_id": self.plan_file_id,
            "parents": list(self.parents),
            "created_at": self.created_at,
            "actor_user_id": self.actor_user_id,
            "actor_device_id": self.actor_device_id,
            "content_hash": self.content_hash,
            "signature": self.signature,
        }
        # Added only when it says something. See the field's own comment: a
        # key on every envelope would rewrite every artifact id in existence.
        if self.memory_id:
            fields["memory_id"] = self.memory_id
        return fields

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact:
        """Rebuild from the wire form. Raises ValueError on a malformed envelope."""
        try:
            return cls(
                protocol_version=int(data["protocol_version"]),
                artifact_id=str(data["artifact_id"]),
                artifact_type=str(data["artifact_type"]),
                organization_id=data.get("organization_id"),
                workspace_id=str(data["workspace_id"]),
                plan_file_id=data.get("plan_file_id"),
                memory_id=data.get("memory_id"),
                parents=tuple(data.get("parents") or ()),
                created_at=str(data["created_at"]),
                actor_user_id=data.get("actor_user_id"),
                actor_device_id=str(data["actor_device_id"]),
                content_hash=str(data["content_hash"]),
                signature=str(data.get("signature", "")),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"Malformed artifact envelope: {e}") from None


def compute_artifact_id(artifact: Artifact) -> str:
    """The id this envelope must carry."""
    return hash_bytes(canonical_bytes(artifact.signing_fields()))


def make_artifact(
    *,
    artifact_type: str,
    workspace_id: str,
    content_hash: str,
    plan_file_id: str | None = None,
    memory_id: str | None = None,
    parents: tuple[str, ...] | list[str] = (),
    organization_id: str | None = None,
    actor_user_id: str | None = None,
    created_at: datetime | None = None,
    signing_key: Any = None,
    actor_device_id: str | None = None,
) -> Artifact:
    """Build and sign an artifact with this device's key.

    Parent order carries no meaning, so parents are sorted: a merge recorded
    from either side yields the same artifact id.
    """
    if artifact_type not in ARTIFACT_TYPES:
        raise ValueError(f"Unknown artifact type: {artifact_type}")
    key = signing_key if signing_key is not None else identity.load_or_create_device_key()
    device = actor_device_id or identity.device_id_for(key.public_key())
    stamp = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

    unsigned = Artifact(
        artifact_type=artifact_type,
        workspace_id=workspace_id,
        content_hash=content_hash,
        actor_device_id=device,
        created_at=stamp.isoformat().replace("+00:00", "Z"),
        organization_id=organization_id,
        plan_file_id=plan_file_id,
        memory_id=memory_id,
        actor_user_id=actor_user_id,
        parents=tuple(sorted(parents)),
    )
    payload = canonical_bytes(unsigned.signing_fields())
    return replace(
        unsigned,
        artifact_id=hash_bytes(payload),
        signature=identity.sign(payload, key),
    )


@dataclass(frozen=True)
class Verdict:
    """Why an artifact was accepted or rejected. Rejections are logged, not raised."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def verify_artifact(
    artifact: Artifact, public_key_b64: str, payload: bytes | None = None
) -> Verdict:
    """Check identity, signature, and (when supplied) the payload itself.

    Never raises: a peer must not be able to crash a verifier with a hostile
    envelope, so every failure comes back as a Verdict carrying its reason
    (PRD §14.4).
    """
    if artifact.protocol_version != PROTOCOL_VERSION:
        return Verdict(False, f"unsupported protocol version {artifact.protocol_version}")
    if artifact.artifact_type not in ARTIFACT_TYPES:
        return Verdict(False, f"unknown artifact type {artifact.artifact_type}")
    try:
        signed = canonical_bytes(artifact.signing_fields())
    except (TypeError, ValueError) as e:
        return Verdict(False, f"envelope is not serializable: {e}")
    if artifact.artifact_id != hash_bytes(signed):
        return Verdict(False, "artifact id does not match its envelope")
    if not identity.verify(public_key_b64, signed, artifact.signature):
        return Verdict(False, "signature does not verify for this device key")
    if payload is not None and hash_bytes(payload) != artifact.content_hash:
        return Verdict(False, "payload does not match content_hash")
    return Verdict(True)


# --- Lineage (PRD §12.3) -----------------------------------------------------
# Ordering is derived from parent links only. Arrival order, timestamps, device
# ids, and human version numbers never decide which version wins.


def ancestors(artifact_id: str, parents_by_id: dict[str, tuple[str, ...]]) -> set[str]:
    """Every id reachable through parent links. Tolerates cycles and gaps."""
    seen: set[str] = set()
    stack = list(parents_by_id.get(artifact_id, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(parents_by_id.get(current, ()))
    return seen


def is_ancestor(candidate: str, of: str, parents_by_id: dict[str, tuple[str, ...]]) -> bool:
    return candidate in ancestors(of, parents_by_id)


def find_heads(parents_by_id: dict[str, tuple[str, ...]]) -> set[str]:
    """Ids no other known artifact descends from."""
    claimed = {parent for parents in parents_by_id.values() for parent in parents}
    return set(parents_by_id) - claimed


def is_conflicted(parents_by_id: dict[str, tuple[str, ...]]) -> bool:
    """True when more than one head exists, i.e. history has diverged.

    Flanner never resolves this silently; the UI and MCP surface it and a
    human or agent merges explicitly (PRD §12.3).
    """
    return len(find_heads(parents_by_id)) > 1


def merge_base(left: str, right: str, parents_by_id: dict[str, tuple[str, ...]]) -> set[str]:
    """Common ancestors of two heads, for presenting a conflict."""
    return ancestors(left, parents_by_id) & ancestors(right, parents_by_id)
