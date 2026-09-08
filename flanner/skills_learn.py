"""Turning work somebody did into a skill, with a person in the middle.

Nothing here happens on its own. Evidence arrives because a person
submitted it or an agent reported it and said which; a proposal is drawn
from evidence a person pointed at; and a proposal becomes installable
only when a person approves that exact draft. There is no path from an
observation to an installed skill that does not pass through somebody
deciding.

Three rules do most of the work.

**Metadata cannot discover repeated prompt content.** Watching skill
invocations sees names and times, not what was being worked on, so
learning needs evidence handed over separately. There is deliberately no
fallback that reads conversation archives; if nobody submitted anything,
there is nothing to propose.

**An approval names a hash, not a proposal.** Editing a draft after
approval changes its hash and the approval no longer covers it. The
alternative — approving an id — means a review of one text authorizing
the installation of another.

**Repetition is a prompt to look, not a verdict.** Three related pieces
of evidence across two sessions opens a proposal for review. It is a
tunable default, not a discovered threshold, and it is said out loud
wherever the number is used.
"""

from __future__ import annotations

import difflib
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from . import memory_guard
from .database import (
    ProjectModel,
    SkillApprovalModel,
    SkillEvidenceModel,
    SkillProposalModel,
)

#: What a piece of evidence is about (LRN-01). Only a procedure can
#: become a skill: a repeated fact belongs in memory, a preference is a
#: setting, and a one-off task is neither.
MEMORY = "memory"
PREFERENCE = "preference"
TASK = "task"
PROCEDURE = "procedure"
KINDS = (MEMORY, PREFERENCE, TASK, PROCEDURE)

#: Who is vouching for it. An agent's account of its own work is weaker
#: and is labelled wherever it is shown.
BY_USER = "user"
BY_AGENT = "agent"

#: What says the work went well. "Nobody complained" is not on this list
#: on purpose: silence is not success evidence.
TEST_PASSED = "test_passed"
USER_ACCEPTED = "user_accepted"
RUBRIC_MET = "rubric_met"
NO_EVIDENCE = "none"
OUTCOMES = (TEST_PASSED, USER_ACCEPTED, RUBRIC_MET, NO_EVIDENCE)

DRAFT = "draft"
APPROVED = "approved"
REJECTED = "rejected"
SUPERSEDED = "superseded"

#: A tunable product default, not a scientifically established threshold.
#: It opens a review; it does not decide anything.
ENOUGH_OCCURRENCES = 3
ENOUGH_SESSIONS = 2

#: Excerpts are somebody's working material. Kept a week unless asked
#: otherwise, because keeping them indefinitely in case a skill gets
#: written one day is not a trade anybody agreed to.
EVIDENCE_DAYS = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def draft_hash(body: str) -> str:
    """What an approval binds to: the bytes, not the row."""
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


# --- evidence -----------------------------------------------------------------


def submit(
    session: Session,
    project: ProjectModel,
    summary: str,
    body: str,
    *,
    kind: str = PROCEDURE,
    source: str = BY_USER,
    session_ref: str = "",
    outcome: str = NO_EVIDENCE,
    outcome_detail: str = "",
    keep_days: int = EVIDENCE_DAYS,
) -> dict[str, Any]:
    """Take one piece of authorized evidence, or refuse it.

    Refuses anything that looks like a credential, through the same guard
    memory uses. Evidence is pasted working material, which is exactly
    where a key ends up by accident, and a stored secret is not undone by
    deleting the row that carried it.
    """
    if kind not in KINDS:
        raise ValueError(f"{kind} is not one of {', '.join(KINDS)}")
    if outcome not in OUTCOMES:
        raise ValueError(f"{outcome} is not one of {', '.join(OUTCOMES)}")

    found = memory_guard.scan(f"{summary}\n{body}")
    if found:
        raise ValueError(memory_guard.describe(found))

    row = SkillEvidenceModel(
        project_id=project.id,
        session_ref=session_ref,
        source=source if source in (BY_USER, BY_AGENT) else BY_AGENT,
        kind=kind,
        summary=summary.strip(),
        body=body,
        outcome=outcome,
        outcome_detail=outcome_detail,
        expires_at=utcnow() + timedelta(days=keep_days) if keep_days else None,
    )
    session.add(row)
    session.commit()
    return _evidence_view(row)


def _evidence_view(row: SkillEvidenceModel) -> dict[str, Any]:
    """A plain dict, because `get_session` hands out a new session each
    call and an ORM instance handed to a caller that then opens another
    one is a detached-instance error waiting for a real subprocess."""
    return {
        "id": str(row.id),
        "summary": row.summary,
        "kind": row.kind,
        "source": row.source,
        "outcome": row.outcome,
        "outcome_detail": row.outcome_detail,
        "session_ref": row.session_ref,
        "expires_at": row.expires_at.isoformat() + "Z" if row.expires_at else None,
    }


def evidence(
    session: Session, project: ProjectModel, *, session_ref: str = "", kind: str | None = None
) -> list[SkillEvidenceModel]:
    """Evidence that has not expired, newest first."""
    query = session.query(SkillEvidenceModel).filter(
        SkillEvidenceModel.project_id == project.id,
        (SkillEvidenceModel.expires_at.is_(None)) | (SkillEvidenceModel.expires_at > utcnow()),
    )
    if session_ref:
        query = query.filter(SkillEvidenceModel.session_ref == session_ref)
    if kind:
        query = query.filter(SkillEvidenceModel.kind == kind)
    return list(query.order_by(SkillEvidenceModel.created_at.desc()).all())


def forget_expired(session: Session) -> dict[str, int]:
    """Drop excerpts past their expiry.

    The proposals drawn from them stay, and say the evidence expired. A
    record that a decision was made on evidence now gone is more use than
    no record at all.
    """
    gone = int(
        session.query(SkillEvidenceModel)
        .filter(
            SkillEvidenceModel.expires_at.isnot(None),
            SkillEvidenceModel.expires_at <= utcnow(),
        )
        .delete(synchronize_session=False)
    )
    session.commit()
    return {"deleted": gone}


# --- what the evidence adds up to ---------------------------------------------


@dataclass(frozen=True)
class Cluster:
    """Evidence that looks like the same procedure, and whether it is enough."""

    topic: str
    items: list[SkillEvidenceModel]
    sessions: int
    with_outcome: int
    eligible: bool
    why: str


def _topic(row: SkillEvidenceModel) -> str:
    """A crude grouping key: the summary, normalised.

    Deliberately crude, and deliberately not a model call. Grouping by
    meaning is the part a person is being asked to check, so a
    clusterer that looked clever here would only make it harder to see
    what was actually compared.
    """
    words = [w for w in row.summary.lower().split() if len(w) > 3]
    return " ".join(sorted(words)[:6]) or row.summary.lower().strip()


def cluster(rows: list[SkillEvidenceModel]) -> list[Cluster]:
    """Group evidence by apparent topic and say which groups are worth a look."""
    groups: dict[str, list[SkillEvidenceModel]] = {}
    for row in rows:
        if row.kind != PROCEDURE:
            continue
        groups.setdefault(_topic(row), []).append(row)

    out = []
    for topic, items in sorted(groups.items()):
        sessions = len({i.session_ref for i in items if i.session_ref})
        with_outcome = sum(1 for i in items if i.outcome != NO_EVIDENCE)
        enough = len(items) >= ENOUGH_OCCURRENCES and sessions >= ENOUGH_SESSIONS
        out.append(
            Cluster(
                topic=topic,
                items=items,
                sessions=sessions,
                with_outcome=with_outcome,
                eligible=enough and with_outcome > 0,
                why=_why(len(items), sessions, with_outcome),
            )
        )
    return sorted(out, key=lambda c: (not c.eligible, -len(c.items)))


def _why(count: int, sessions: int, with_outcome: int) -> str:
    missing = []
    if count < ENOUGH_OCCURRENCES:
        missing.append(f"{count} of {ENOUGH_OCCURRENCES} occurrences")
    if sessions < ENOUGH_SESSIONS:
        missing.append(f"{sessions} of {ENOUGH_SESSIONS} sessions")
    if not with_outcome:
        missing.append("nothing saying it worked")
    if missing:
        return "not yet: " + ", ".join(missing)
    return (
        f"{count} occurrences across {sessions} sessions, {with_outcome} with success "
        "evidence. A default worth reviewing, not a finding."
    )


# --- proposals ----------------------------------------------------------------


def propose(
    session: Session,
    project: ProjectModel,
    skill_name: str,
    body: str,
    *,
    action: str = "create",
    base_hash: str = "",
    provenance: list[str] | None = None,
    rationale: str = "",
    created_by: str = BY_USER,
) -> dict[str, Any]:
    """Draft a create, update or merge for review.

    A proposal with no provenance is refused. The reviewer's job is to
    check the claim against what it came from, and a draft that cannot
    point anywhere makes that impossible — which is the shape a
    hallucinated skill arrives in.
    """
    if action not in ("create", "update", "merge"):
        raise ValueError(f"{action} is not create, update or merge")
    if not provenance:
        raise ValueError("a proposal must name the evidence it came from")

    found = memory_guard.scan(body)
    if found:
        raise ValueError(memory_guard.describe(found))

    row = SkillProposalModel(
        project_id=project.id,
        action=action,
        skill_name=skill_name,
        base_hash=base_hash,
        draft_body=body,
        draft_hash=draft_hash(body),
        provenance=",".join(provenance),
        rationale=rationale,
        created_by=created_by,
    )
    session.add(row)
    session.commit()
    return _proposal_view(row)


def _proposal_view(row: SkillProposalModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "skill": row.skill_name,
        "action": row.action,
        "state": row.state,
        "draft_hash": row.draft_hash,
        "provenance": [p for p in row.provenance.split(",") if p],
    }


def revise(session: Session, proposal_id: str, body: str) -> dict[str, Any]:
    """Edit a draft, which invalidates any approval it already had.

    That invalidation is the point. An approval covers a hash, so editing
    after approval leaves the approval behind on the text that was
    actually read.
    """
    row = _proposal(session, proposal_id)
    found = memory_guard.scan(body)
    if found:
        raise ValueError(memory_guard.describe(found))

    row.draft_body = body
    row.draft_hash = draft_hash(body)
    row.state = DRAFT
    row.updated_at = utcnow()
    session.commit()
    return _proposal_view(row)


def _proposal(session: Session, proposal_id: str) -> SkillProposalModel:
    try:
        found = (
            session.query(SkillProposalModel)
            .filter_by(id=uuid.UUID(str(proposal_id)))
            .one_or_none()
        )
    except ValueError:
        found = None
    if found is None:
        raise ValueError(f"no proposal with id {proposal_id}")
    return found


def diff(session: Session, proposal_id: str, current: str = "") -> list[str]:
    """The draft against what it would replace, as unified diff lines."""
    row = _proposal(session, proposal_id)
    return list(
        difflib.unified_diff(
            current.splitlines(),
            row.draft_body.splitlines(),
            fromfile=f"{row.skill_name} (now)",
            tofile=f"{row.skill_name} (proposed)",
            lineterm="",
        )
    )


def decide(
    session: Session,
    proposal_id: str,
    decision: str,
    *,
    actor: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Approve or reject one exact draft.

    The approval records the hash it read. `authorized` later compares
    that hash to the draft as it stands, so an edit between approval and
    installation is caught rather than shipped.
    """
    if decision not in (APPROVED, REJECTED):
        raise ValueError(f"{decision} is not {APPROVED} or {REJECTED}")

    row = _proposal(session, proposal_id)
    session.add(
        SkillApprovalModel(
            proposal_id=row.id,
            approved_hash=row.draft_hash,
            actor=actor,
            decision=decision,
            note=note,
        )
    )
    row.state = decision
    row.updated_at = utcnow()
    session.commit()
    return {
        "proposal_id": str(row.id),
        "skill": row.skill_name,
        "decision": decision,
        "approved_hash": row.draft_hash,
    }


def authorized(session: Session, proposal_id: str) -> tuple[bool, str]:
    """Whether this draft, as it stands, may be installed.

    Two separate questions, and both have to hold: somebody approved, and
    what they approved is what is there now.
    """
    row = _proposal(session, proposal_id)
    latest = (
        session.query(SkillApprovalModel)
        .filter_by(proposal_id=row.id)
        .order_by(SkillApprovalModel.created_at.desc())
        .first()
    )
    if latest is None:
        return False, "nobody has approved this"
    if latest.decision != APPROVED:
        return False, f"the last decision on it was {latest.decision}"
    if latest.approved_hash != row.draft_hash:
        return False, "the draft changed after it was approved; it needs another look"
    return True, "approved"


def proposals(
    session: Session, project: ProjectModel | None = None, state: str | None = None
) -> list[dict[str, Any]]:
    """Proposals, newest first, each with whether it may be installed."""
    query = session.query(SkillProposalModel)
    if project is not None:
        query = query.filter(SkillProposalModel.project_id == project.id)
    if state:
        query = query.filter(SkillProposalModel.state == state)

    out = []
    for row in query.order_by(SkillProposalModel.created_at.desc()).all():
        may, why = authorized(session, str(row.id))
        provenance = [p for p in row.provenance.split(",") if p]
        kept = (
            session.query(SkillEvidenceModel)
            .filter(SkillEvidenceModel.id.in_([uuid.UUID(p) for p in provenance]))
            .count()
            if provenance
            else 0
        )
        out.append(
            {
                "id": str(row.id),
                "skill": row.skill_name,
                "action": row.action,
                "state": row.state,
                "draft_hash": row.draft_hash,
                "created_by": row.created_by,
                "rationale": row.rationale,
                "installable": may,
                "why": why,
                "evidence_count": len(provenance),
                # Provenance survives its evidence; the count says how much
                # of it a reviewer can still actually read.
                "evidence_available": kept,
                "created_at": row.created_at.isoformat() + "Z",
            }
        )
    return out


def review(session: Session, proposal_id: str, current: str = "") -> dict[str, Any]:
    """Everything a person needs in front of them to decide."""
    row = _proposal(session, proposal_id)
    provenance = [p for p in row.provenance.split(",") if p]
    kept = (
        session.query(SkillEvidenceModel)
        .filter(SkillEvidenceModel.id.in_([uuid.UUID(p) for p in provenance]))
        .all()
        if provenance
        else []
    )
    may, why = authorized(session, proposal_id)
    return {
        "id": str(row.id),
        "skill": row.skill_name,
        "action": row.action,
        "state": row.state,
        "draft_hash": row.draft_hash,
        "draft_body": row.draft_body,
        "rationale": row.rationale,
        "created_by": row.created_by,
        "installable": may,
        "why": why,
        "diff": diff(session, proposal_id, current),
        "evidence": [
            {
                "id": str(e.id),
                "summary": e.summary,
                "source": e.source,
                "outcome": e.outcome,
                "outcome_detail": e.outcome_detail,
                "session_ref": e.session_ref,
            }
            for e in kept
        ],
        "evidence_expired": len(provenance) - len(kept),
        "decisions": [
            {
                "decision": a.decision,
                "actor": a.actor,
                "note": a.note,
                "approved_hash": a.approved_hash,
                "at": a.created_at.isoformat() + "Z",
                "still_covers_the_draft": a.approved_hash == row.draft_hash,
            }
            for a in session.query(SkillApprovalModel)
            .filter_by(proposal_id=row.id)
            .order_by(SkillApprovalModel.created_at.desc())
            .all()
        ],
        "notes": [
            "An agent's account of its own work is weaker evidence than a "
            "person's, and is labelled by source above."
            if any(e.source == BY_AGENT for e in kept)
            else "",
            f"Repetition opens a review at {ENOUGH_OCCURRENCES} occurrences across "
            f"{ENOUGH_SESSIONS} sessions. That is a product default, not a "
            "discovered threshold.",
        ],
    }
