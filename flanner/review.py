"""Recording review: propose, decide, and advance the baseline (PRD §12.5).

The projection side of review already existed and was read-only. This is the
write side: it creates the signed events, stores them as artifacts, and
emits the accepted-head transition when a decision satisfies the policy.

Approving is deliberately not the same as accepting. A decision records
what a reviewer thought; only an accepted-head transition moves the team's
baseline, and only when the approvals it cites meet the workspace policy.
Doing both in one call is a convenience, not a shortcut: the transition is
still a separate signed event that any peer can validate on its own.

**On authorization.** Roles here are advisory, not enforced. Real roles
arrive as signed workspace capabilities from the control plane (§11.3),
which does not exist yet, so a local roles map is a placeholder: anyone who
can edit it can promote themselves. It is enough to exercise the machinery
and shape the UX, and it is not a security boundary. Nothing downstream
assumes otherwise, because every event is signed and re-validated by the
peer that receives it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from . import anchors, authz, workflow
from .assurance import load_review_events
from .database import (
    PlanFileModel,
    ProjectModel,
    get_version,
    list_versions,
    save_envelope,
)
from .plan_ops import workspace_id_for
from .storage import load_plan_file
from .utils import utcnow
from .workflow import (
    APPROVE,
    MAINTAINER,
    WITHDRAW,
    Event,
    Policy,
    WorkflowState,
    local_roles,
)


def may_review(role: str) -> bool:
    """Whether a workspace role may decide on proposals."""
    return role in workflow.MAY_REVIEW


@dataclass(frozen=True)
class ReviewResult:
    """What a review action recorded, and whether it moved the baseline."""

    event: Event
    accepted: Event | None = None
    reason: str = ""

    @property
    def advanced_baseline(self) -> bool:
        return self.accepted is not None


def save_event(session: Session, event: Event, plan_file_id: str) -> None:
    """Store a signed event as an artifact, payload alongside.

    Stored in the canonical form the content hash was taken over. Plain
    `json.dumps` writes a space after each separator, so a peer hashed
    different bytes and refused every review artifact with "payload does
    not match content_hash": retirements, proposals, decisions and
    comments all stopped at the first device that tried to send one.
    """
    artifact = event.artifact
    save_envelope(
        session,
        artifact,
        plan_file_id=plan_file_id,
        payload=workflow.stored_payload(event.payload),
    )


#: Where a decision was recorded. An agent relays a person's decision; it is
#: not the person, so the two are told apart where authority is at stake.
PERSON = "person"
AGENT = "agent"


def status(
    session: Session,
    *,
    plan_file: PlanFileModel,
    project: ProjectModel | None = None,
    roles: dict[str, str] | None = None,
    policy: Policy | None = None,
    authorization: authz.Authorization | None = None,
) -> WorkflowState:
    """Project the current review state for a plan.

    Without a project there is no workspace to resolve an entitlement
    against, so the local placeholder stands in. Callers that hold one
    should pass it, or a joined workspace will read as advisory here while
    the assurance verdict enforces it.
    """
    if authorization is None and project is not None:
        authorization = authz.resolve(project)
    if roles is None:
        roles = authorization.roles if authorization is not None else local_roles()
    if policy is None:
        policy = authorization.policy if authorization is not None else workflow.DEFAULT_POLICY
    return workflow.project(
        load_review_events(session, str(plan_file.id)),
        roles,
        policy,
        verifier=authorization.verifier if authorization is not None else None,
    )


def propose(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    artifact_id: str | None = None,
    message: str = "",
    actor: str | None = None,
    roles: dict[str, str] | None = None,
    policy: Policy | None = None,
) -> ReviewResult:
    """Offer a version for review.

    Defaults to the plan's newest version, and records the baseline it was
    written against so the proposal can later be recognised as stale if the
    baseline moves first (§12.5.4).
    """
    target = artifact_id
    if target is None:
        version = get_version(session, plan_file.id, None)
        if version is None or not version.artifact_id:
            raise ValueError("this plan has no signed version to propose")
        target = version.artifact_id

    authorization = authz.resolve(project, actor=actor)
    effective_roles = roles if roles is not None else authorization.roles
    # Refuse before writing. Projection would drop an unauthorized proposal
    # anyway - it has to, because the same rule governs events arriving from
    # peers - but a local caller deserves to be told, rather than watch the
    # command succeed and the proposal never appear.
    _require(effective_roles, authorization, workflow.MAY_PROPOSE, "propose on this plan")
    policy = policy or authorization.policy

    state = status(
        session,
        plan_file=plan_file,
        roles=effective_roles,
        policy=policy,
        authorization=authorization,
    )
    event = workflow.make_proposal(
        workspace_id=workspace_id_for(project),
        plan_file_id=str(plan_file.id),
        target_artifact_id=target,
        base_accepted_event_ids=state.accepted_event_ids,
        message=message,
        policy=policy,
        actor_user_id=authorization.actor,
    )
    save_event(session, event, str(plan_file.id))
    session.commit()
    return ReviewResult(event=event)


def comment(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    quote: str,
    body: str,
    occurrence: int = 0,
    version: int | None = None,
    actor: str | None = None,
    roles: dict[str, str] | None = None,
) -> ReviewResult:
    """Leave a note against a quotation in a plan.

    Refused before writing rather than after. The projection would drop an
    unauthorised comment anyway - it has to, because the same rule governs
    events arriving from peers - but somebody typing at a prompt deserves to
    be told, not to watch the command succeed and the note never appear.
    """
    text = anchors.clip(quote)
    if not text:
        raise ValueError("a comment has to quote something")
    if not body.strip():
        raise ValueError("a comment has to say something")

    target = get_version(session, plan_file.id, version)
    if target is None:
        raise ValueError("that version does not exist")

    # The quotation has to be in the version being commented on. Catching it
    # here turns a note that would silently never appear into a refusal that
    # explains itself.
    try:
        _, source = load_plan_file(target.file_path)
    except (FileNotFoundError, OSError) as e:
        raise ValueError(f"v{target.version} is not readable: {e}") from None
    if anchors.occurrences(text, source) == 0:
        raise ValueError(f"that text is not in v{target.version} of this plan")

    authorization = authz.resolve(project, actor=actor)
    effective_roles = roles if roles is not None else authorization.roles
    _require(effective_roles, authorization, workflow.MAY_COMMENT, "comment on this plan")

    event = workflow.make_comment(
        workspace_id=workspace_id_for(project),
        plan_file_id=str(plan_file.id),
        target_artifact_id=target.artifact_id or "",
        target_version=target.version,
        quote=text,
        body=body.strip(),
        occurrence=occurrence,
        actor_user_id=authorization.actor,
    )
    save_event(session, event, str(plan_file.id))
    session.commit()
    return ReviewResult(event=event)


def retire(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    reason: str = "",
    restore: bool = False,
    actor: str | None = None,
    roles: dict[str, str] | None = None,
) -> ReviewResult:
    """Ask peers to stop showing this plan, or to show it again.

    Never called a deletion in code or in prose. Every artifact survives,
    every signature still verifies, and a peer that was offline when this
    was signed holds the content regardless. What travels is a claim, and
    a device only honours it once it has actually received it.

    Maintainer only. Hiding a plan for a whole team is closer to deciding
    than to editing.
    """
    authorization = authz.resolve(project, actor=actor)
    effective_roles = roles if roles is not None else authorization.roles
    _require(
        effective_roles,
        authorization,
        workflow.MAY_RETIRE,
        "restore this plan" if restore else "retire this plan",
    )

    event = workflow.make_tombstone(
        workspace_id=workspace_id_for(project),
        plan_file_id=str(plan_file.id),
        reason=reason,
        restored=restore,
        actor_user_id=authorization.actor,
    )
    save_event(session, event, str(plan_file.id))
    session.commit()
    return ReviewResult(event=event)


def import_external(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    reviewer: str,
    notes: list[dict[str, Any]],
    reviewed_version: int | None = None,
    source: str = "packet",
    actor: str | None = None,
) -> ReviewResult:
    """Record notes that came back from somebody outside the mesh.

    Anchored against the version the packet was built from, not the newest
    one. A reviewer read a particular text and their notes belong to it; if
    the plan has moved on, that is worth seeing rather than papering over.
    """
    # The version the reviewer actually read, not the newest one. They
    # marked up a particular text; recording their notes against a later
    # revision they never saw would misattribute every one of them.
    version = get_version(session, plan_file.id, reviewed_version)
    if version is None and reviewed_version is not None:
        version = get_version(session, plan_file.id, None)
    if version is None:
        raise ValueError("this plan has no versions to attach review to")

    # A local plan that has never joined a workspace has no signed artifact,
    # and refusing on that basis would make this command useless for exactly
    # the people most likely to need it. The event carries the version
    # number either way; the signature that matters is this device's, which
    # says where the notes came from.
    authorization = authz.resolve(project, actor=actor)
    event = workflow.make_external_review(
        workspace_id=workspace_id_for(project),
        plan_file_id=str(plan_file.id),
        target_artifact_id=version.artifact_id or "",
        target_version=version.version,
        reviewer=reviewer,
        notes=notes,
        source=source,
        actor_user_id=authorization.actor,
    )
    save_event(session, event, str(plan_file.id))
    session.commit()
    return ReviewResult(event=event)


def decide(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    proposal_id: str,
    action: str,
    actor: str | None = None,
    roles: dict[str, str] | None = None,
    policy: Policy | None = None,
    surface: str = PERSON,
    confirmation: str | None = None,
) -> ReviewResult:
    """Record a decision, and advance the baseline if policy is now satisfied.

    The decision names the exact version the reviewer saw, so it can never
    be replayed against different content. Where review is enforced, an
    approval carries `confirmation`: the console's signed record that the
    approver agreed to it.
    """
    authorization = authz.resolve(project, actor=actor)
    acting_as = authorization.actor
    effective_roles = roles if roles is not None else authorization.roles
    policy = policy or authorization.policy
    before = status(
        session,
        plan_file=plan_file,
        roles=effective_roles,
        policy=policy,
        authorization=authorization,
    )
    proposal = before.proposals.get(proposal_id)
    if proposal is None:
        raise ValueError(f"no proposal {proposal_id} on this plan")

    # Withdrawing is the proposer's own act, so it needs no review role.
    if action != WITHDRAW:
        _require(effective_roles, authorization, workflow.MAY_REVIEW, "review this plan")

    # Where review is enforced, an approval moves a baseline the whole
    # workspace reads, so it is recorded on a surface a person operates.
    # Rejecting, asking for changes and withdrawing stay open to an agent:
    # none of them grants anything. Solo review is advisory and binds nobody,
    # so an agent may approve there.
    if action == APPROVE and surface == AGENT and authorization.enforced:
        raise PermissionError(
            "cannot approve through an agent where review is enforced: "
            "approve with `flanner review decide`"
        )

    # Refused here rather than recorded and then dropped by the projection,
    # which would read as an approval that silently did nothing.
    if (
        action == APPROVE
        and proposal.proposer == acting_as
        and not workflow.may_self_approve(policy, effective_roles, acting_as)
    ):
        raise PermissionError(
            "cannot approve your own proposal: another maintainer here has to review it"
        )

    event = workflow.make_decision(
        workspace_id=workspace_id_for(project),
        plan_file_id=str(plan_file.id),
        proposal_id=proposal_id,
        target_artifact_id=proposal.target_artifact_id,
        action=action,
        actor_user_id=acting_as,
        confirmation=confirmation,
    )
    # The same check the projection makes, run before anything is stored.
    # Nothing on this machine can show a person approved, since an agent
    # with a shell runs the same commands they do. A confirmation signed by
    # the console, for a browser signed in as the approver, can.
    refusal = authorization.verifier.refusal(event) if authorization.verifier else ""
    if refusal:
        raise PermissionError(f"cannot record this approval: {refusal}")
    save_event(session, event, str(plan_file.id))
    session.commit()

    if action != APPROVE:
        return ReviewResult(event=event, reason=f"recorded {action}")

    accepted, reason = _try_accept(
        session,
        project=project,
        plan_file=plan_file,
        proposal_id=proposal_id,
        actor=acting_as,
        roles=effective_roles,
        policy=policy,
        authorization=authorization,
    )
    return ReviewResult(event=event, accepted=accepted, reason=reason)


def needs_confirmation(
    authorization: authz.Authorization, proposal: workflow.ProposalView | None
) -> bool:
    """Whether approving this proposal waits on the console first.

    Only where review is enforced, and only when the approval would
    otherwise stand, so nobody confirms something that is then refused.
    """
    return (
        authorization.enforced
        and proposal is not None
        and authorization.role in workflow.MAY_REVIEW
        and (
            proposal.proposer != authorization.actor
            or workflow.may_self_approve(
                authorization.policy, authorization.roles, authorization.actor
            )
        )
    )


def _require(
    roles: dict[str, str], authorization: authz.Authorization, permitted: frozenset[str], what: str
) -> None:
    """Stop early when the resolved authorization does not allow this.

    The message names the reason the entitlement gave, because "you may not
    do that" without saying why is the least useful refusal there is.
    """
    held = roles.get(authorization.actor)
    if held in permitted:
        return
    detail = authorization.reason or (
        f"{authorization.actor} is a {held} here, and this needs " + " or ".join(sorted(permitted))
        if held
        else f"{authorization.actor} holds no role here"
    )
    raise PermissionError(f"cannot {what}: {detail}")


def _try_accept(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    proposal_id: str,
    actor: str,
    roles: dict[str, str],
    policy: Policy,
    authorization: authz.Authorization,
) -> tuple[Event | None, str]:
    """Emit an accepted-head transition when the approvals now justify one.

    Returns the transition and why, or None and the reason it was withheld,
    so a caller can always explain the outcome to a human.
    """
    state = status(
        session, plan_file=plan_file, roles=roles, policy=policy, authorization=authorization
    )
    proposal = state.proposals.get(proposal_id)
    if proposal is None:
        return None, "proposal is no longer projected"
    if proposal.state == workflow.ACCEPTED:
        return None, "already the accepted baseline"
    # Checked before the count, which would otherwise read "0 of 1" and
    # hide that the approval was recorded and is only waiting on a renewal.
    if authorization.roster_in_grace:
        return None, authz.RENEW_TO_COUNT_APPROVALS
    if len(proposal.approvals) < policy.approvals_required:
        return None, (
            f"{len(proposal.approvals)} of {policy.approvals_required} required approvals recorded"
        )
    if roles.get(actor) != MAINTAINER:
        return None, f"{actor} may not advance the baseline"
    if state.conflicted:
        return None, "the baseline is contested and must be merged first"

    decisions = [
        event.event_id
        for event in load_review_events(session, str(plan_file.id))
        if event.payload.get("proposal_id") == proposal_id
        and event.payload.get("action") == APPROVE
        and event.actor in proposal.approvals
    ]
    accepted = workflow.make_accepted_head(
        workspace_id=workspace_id_for(project),
        plan_file_id=str(plan_file.id),
        target_artifact_id=proposal.target_artifact_id,
        proposal_id=proposal_id,
        decision_event_ids=decisions,
        # Citing the heads we observed keeps this a descendant rather than a
        # second root, which is what would otherwise look like a conflict.
        predecessor_event_ids=state.accepted_event_ids,
        policy=policy,
        actor_user_id=actor,
    )
    save_event(session, accepted, str(plan_file.id))
    _point_at(session, plan_file, proposal.target_artifact_id)
    session.commit()
    return accepted, "baseline advanced"


def _point_at(session: Session, plan_file: PlanFileModel, target_artifact_id: str) -> None:
    """Move the plan's current version to the artifact just accepted.

    An arriving version deliberately does not move the pointer, so without
    this the baseline everyone agreed on was recorded in the event log and
    invisible everywhere a person or an agent actually looks — `flanner
    show`, the web UI, the MCP tools all read `current_version`. Accepting
    is the decision the pointer was being held for.

    It may move backwards. Accepting an earlier artifact as the baseline is
    a revert, and refusing to follow one would leave the pointer somewhere
    the team explicitly rejected. No file is rewritten either way; every
    version stays on disk.
    """
    for version in list_versions(session, plan_file.id):
        if version.artifact_id == target_artifact_id:
            plan_file.current_version = version.version
            plan_file.updated_at = utcnow()
            return
