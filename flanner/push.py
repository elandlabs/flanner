"""Receiving artifacts a peer sends without being asked.

Pull answers a question. Push accepts data, which is a genuinely wider
surface, so the rules that make it safe are gathered here rather than
scattered through the transport.

Three of them matter:

**The sender is not the author.** A pushed artifact is verified against the
author's key from the organisation keyring, exactly as on pull. A hostile
peer may offer whatever it likes; anything it did not legitimately receive
fails verification and is dropped. Push changes who starts the exchange, not
who is believed.

**The role has to cover what was sent.** Reading a workspace and writing to
it are different permissions. A `READER` who pushes a plan version is
refused per artifact, not per request, so one over-reaching item in a batch
does not discard the rest.

**A live entitlement, not merely a usable one.** Elsewhere an expired
entitlement stays usable through a grace window, because punishing somebody
for a weekend offline is not security. That reasoning does not reach here:
a device serving a push request is online by definition, so it cannot claim
it was unable to check. Requiring `VALID` cuts the window in which a
revoked device can still write from the grace period plus a lifetime — over
a week — down to at most one entitlement lifetime.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import artifacts, sync, workflow
from .database import get_artifact

#: Which role a sender needs for each kind of artifact. Comments and
#: decisions are cheap and reversible; versions and proposals are the ones
#: that can move what a colleague's tools read, so they need `EDITOR`.
#:
#: An artifact type absent from this mapping is refused rather than allowed
#: by default — a new type should have to say who may send it.
REQUIRED_ROLE: dict[str, frozenset[str]] = {
    artifacts.COMMENT: workflow.MAY_COMMENT,
    artifacts.REVIEW_DECISION: workflow.MAY_COMMENT,
    artifacts.REVIEW_EXTERNAL: workflow.MAY_COMMENT,
    artifacts.FRESHNESS_EVIDENCE: workflow.MAY_COMMENT,
    artifacts.PLAN_VERSION: workflow.MAY_PROPOSE,
    artifacts.PLAN_HEAD: workflow.MAY_PROPOSE,
    artifacts.REVIEW_PROPOSAL: workflow.MAY_PROPOSE,
    artifacts.ACCEPTED_HEAD: workflow.MAY_PROPOSE,
    artifacts.PLAN_TOMBSTONE: workflow.MAY_RETIRE,
    artifacts.ISSUE_LINK: workflow.MAY_PROPOSE,
    artifacts.COLLAB_CHECKPOINT: workflow.MAY_PROPOSE,
    artifacts.ATTACHMENT_MANIFEST: workflow.MAY_PROPOSE,
    # Sharing a memory is not proposing a change to anybody's plan, so it
    # asks for the same role as a comment rather than the one that can move
    # what a colleague's tools read. Someone who may say "this is wrong" in
    # a review may also say "the staging cluster rebuilds on Sundays".
    artifacts.MEMORY_RECORD: workflow.MAY_COMMENT,
    artifacts.MEMORY_TOMBSTONE: workflow.MAY_COMMENT,
}

#: Artifact types that only travel when the entitlement includes memory
#: sync. Kept apart from the role table because the two questions are
#: different: the role says who, the feature says whether at all. The list
#: itself lives in `sync`, which also needs it to decide what to
#: materialise, and one list is the only way the two stay in step.
MEMORY_TYPES = sync.MEMORY_TYPES


def may_send(artifact_type: str, role: str) -> bool:
    """Whether a peer holding this role may push this kind of artifact."""
    return role in REQUIRED_ROLE.get(artifact_type, frozenset())


@dataclass
class RateLimiter:
    """How often one device may push, tracked in memory.

    In memory on purpose. The limit protects against a colleague's runaway
    script, not against a determined attacker who could restart our daemon
    — and that attacker already needs a valid entitlement and a signing key.
    Persisting this would buy very little and cost a schema.
    """

    window: float = sync.PUSH_WINDOW
    limit: int = sync.MAX_PUSHES_PER_WINDOW
    _seen: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, device_id: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        recent = [t for t in self._seen.get(device_id, ()) if moment - t < self.window]
        if len(recent) >= self.limit:
            # Not recorded. Otherwise a device hammering the limit would keep
            # extending its own lockout, which turns a rate limit into a ban.
            self._seen[device_id] = recent
            return False
        recent.append(moment)
        self._seen[device_id] = recent
        return True


@dataclass
class Cooldown:
    """A floor between keyring refreshes, counted across every peer.

    Not per device, on purpose. The keyring is per organisation, so one
    refresh answers for every unknown author at once — and counting per
    device would let a peer reset the floor by inventing a new id, which is
    exactly the move this exists to stop.
    """

    window: float = sync.KEYRING_COOLDOWN
    _last: float | None = None

    def allow(self, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        if self._last is not None and moment - self._last < self.window:
            return False
        self._last = moment
        return True


def check_batch(items: list[dict[str, Any]]) -> str | None:
    """Why this push is too big, or None if it is within the caps.

    Checked before anything is decoded or verified, because the point of a
    cap is to refuse work rather than to do it and then complain.
    """
    if not items:
        return "a push has to carry something"
    if len(items) > sync.MAX_PUSH_BATCH:
        return f"at most {sync.MAX_PUSH_BATCH} artifacts per push"
    total = sum(len(str(item.get("payload") or "")) for item in items)
    if total > sync.MAX_PUSH_BYTES:
        return f"at most {sync.MAX_PUSH_BYTES} bytes per push"
    return None


def accept(
    session: Any,
    items: list[dict[str, Any]],
    *,
    workspace_id: str,
    role: str,
    resolve_key: Any,
    refresh_keys: Any = None,
    cooldown: Cooldown | None = None,
    memory_sync: bool = True,
) -> sync.SyncReport:
    """Take in what a peer pushed, artifact by artifact.

    Every check that `sync_from_peer` applies to a fetched artifact applies
    here too, and one more: the sender's role has to cover the kind of thing
    they sent. Refusals are per artifact so a batch is not all-or-nothing.

    An artifact whose parents this device lacks is accepted and held. The
    graph tolerates gaps — `heads` simply does not advance past the hole
    until the missing link arrives, which the next catch-up pull supplies.
    Fetching the chain backwards on arrival would let a sender make us do
    unbounded work by pushing one deep orphan.

    ``refresh_keys`` is called at most once per batch, and only when a
    refusal says the author is unknown — a new teammate's first push, which
    would otherwise fail until this device next signed in. It is injected
    rather than imported so this module keeps its distance from the network:
    a reachability test asserts that no read path can reach ``account``, and
    the shortcut it guards against is exactly one import away from here. It
    returns a fresh resolver, because the one passed in is bound to the
    session as it was before the fetch and would still not know the key.
    """
    report = sync.SyncReport()
    unknown: list[tuple[dict[str, Any], bytes | None, str]] = []

    def ingest(envelope: dict[str, Any], payload: bytes | None, resolver: Any) -> Any:
        return sync.ingest_artifact(session, envelope, payload, resolver)

    for item in items:
        envelope = item.get("envelope")
        if not isinstance(envelope, dict):
            report.rejected.append(("<unknown>", "malformed push item"))
            continue
        artifact_id = str(envelope.get("artifact_id", "<unknown>"))

        if envelope.get("workspace_id") != workspace_id:
            report.rejected.append((artifact_id, "artifact belongs to a different workspace"))
            continue

        artifact_type = str(envelope.get("artifact_type") or "")
        if artifact_type in MEMORY_TYPES and not memory_sync:
            # An organization switched this off, or nobody is paying for it.
            # Refused per artifact so a batch carrying both plans and memory
            # still delivers the plans.
            report.rejected.append((artifact_id, "this entitlement does not include memory sync"))
            continue
        if not may_send(artifact_type, role):
            report.rejected.append((artifact_id, f"a {role} may not push a {artifact_type}"))
            continue

        # Cheap and worth doing before verification, which is not cheap.
        if get_artifact(session, artifact_id) is not None:
            report.already_held.append(artifact_id)
            continue

        blob = item.get("payload")
        payload = blob.encode("utf-8") if isinstance(blob, str) else None
        verdict = ingest(envelope, payload, resolve_key)
        if verdict:
            report.accepted.append(artifact_id)
        elif sync.is_unknown_author(verdict):
            # Held back rather than refused outright: this is the one
            # failure a keyring refresh could turn into an acceptance.
            unknown.append((envelope, payload, artifact_id))
        else:
            report.rejected.append((artifact_id, verdict.reason))

    if not unknown:
        return report

    fresh = _relearn(refresh_keys, cooldown)
    for envelope, payload, artifact_id in unknown:
        # Without a fresh resolver there is nothing new to try, so the
        # original refusal stands and is reported as it was.
        verdict = ingest(envelope, payload, fresh) if fresh else None
        if verdict is not None and verdict:
            report.accepted.append(artifact_id)
        else:
            reason = verdict.reason if verdict is not None else _stale_reason(envelope)
            report.rejected.append((artifact_id, reason))
    return report


def _stale_reason(envelope: dict[str, Any]) -> str:
    return (
        f"{sync.UNKNOWN_AUTHOR} {envelope.get('actor_device_id', '<unknown>')}"
        " (sign in again if this is a new teammate)"
    )


def _relearn(refresh_keys: Any, cooldown: Cooldown | None) -> Any:
    """Fetch the organisation keyring once, if we are allowed to right now.

    Every failure answers None, which simply leaves the original refusal in
    place. A control plane that is unreachable must not turn a push we could
    merely not verify into an error on the sender's screen.
    """
    if refresh_keys is None:
        return None
    if cooldown is not None and not cooldown.allow():
        return None
    try:
        return refresh_keys()
    except Exception:  # noqa: BLE001 - any failure here is just "no new keys"
        return None


__all__ = [
    "REQUIRED_ROLE",
    "Cooldown",
    "RateLimiter",
    "accept",
    "check_batch",
    "may_send",
]
