"""Proposal, review, and the accepted baseline (PRD §12.5).

A plan version never carries a mutable ``draft``/``approved`` field. The
version is immutable content; its workflow state is a *projection* of signed,
append-only events. That separation is what lets the same version be reviewed
twice, keeps a rejected version verifiable, and survives peers receiving valid
events in different orders.

Three heads exist at once, and conflating them is the mistake this module
exists to prevent:

- **working head** — what someone is editing. No team authority.
- **proposal head** — an immutable version offered for review. Several may be open.
- **accepted head** — the team's baseline. This is what a plain read returns,
  and what an agent implements from.

Synchronizing a proposal therefore never advances the baseline. Only an
accepted-head transition does, and only when the policy's approvals are
present. Nothing here is decided by timestamp, arrival order, device id, or
human version number: two disconnected peers that both accept produce an
explicit conflict rather than a silent winner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import artifacts
from .artifacts import Artifact

# Roles a workspace capability may carry (PRD §11.3).
READER = "reader"
COMMENTER = "commenter"
EDITOR = "editor"
MAINTAINER = "maintainer"

# Decisions a reviewer may record.
APPROVE = "approve"
REJECT = "reject"
REQUEST_CHANGES = "request_changes"
WITHDRAW = "withdraw"
DECISION_ACTIONS = frozenset({APPROVE, REJECT, REQUEST_CHANGES, WITHDRAW})

# What a policy does about a concern: surface it, or refuse to proceed.
WARN = "warn"
BLOCK = "block"

# Projected proposal states (the arrows in PRD §12.5.3).
OPEN = "open"
CHANGES_REQUESTED = "changes_requested"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
ACCEPTED = "accepted"
SUPERSEDED = "superseded"
STALE = "stale"


@dataclass(frozen=True)
class Policy:
    """Who may advance the baseline, and on how much agreement.

    The first paid beta ships the default: editors propose, one maintainer
    approval accepts, and a maintainer may approve their own proposal unless
    the workspace asks for independent review. The identifier travels in every
    event so a later policy cannot silently re-judge old history.
    """

    policy_id: str = "default-v1"
    approvals_required: int = 1
    allow_self_approval: bool = True
    # Assurance rules (PRD §20). Warn-only is the first-beta default: a
    # stale plan is surfaced to the agent but does not stop it. A workspace
    # that wants enforcement flips these without a protocol change.
    stale_plans: str = WARN
    unreviewed_plans: str = WARN


# The actor recorded when nobody says who is acting. Team deployments carry
# a real user id in the capability; locally there is one person.
LOCAL_ACTOR = "local"


class LocalRoles(dict[str, str]):
    """Every actor is a maintainer, because there is no authority to ask.

    A plain map would have to guess which names to include, and would then
    silently reject anyone else as unauthorized: an agent calling itself
    "claude" and a human calling themselves "user" would each be refused
    for having the wrong name, which reads as a permissions decision when
    no permissions exist. Answering the same way for everyone makes the
    absence of authorization visible instead of arbitrary.
    """

    def get(self, key: object, default: object = None) -> str:
        return MAINTAINER

    def __missing__(self, key: object) -> str:
        return MAINTAINER


def local_roles(actor: str = LOCAL_ACTOR) -> dict[str, str]:
    """Placeholder role map for a workspace with no control plane.

    Local review gates nothing: it is a rehearsal of the workflow, not an
    authorization check. Replaced wholesale by signed capabilities once a
    workspace is joined to an organization.
    """
    return LocalRoles()


DEFAULT_POLICY = Policy()

# Exported so the write surface can refuse before recording an event that
# projection would only discard. One definition, so the two cannot drift.
#: Leaving a note is the lightest thing anybody can do to a plan: it adds
#: an opinion and changes no text. It is the one capability the reader tier
#: above `reader` was named for, and until now granted nothing.
MAY_COMMENT = frozenset({COMMENTER, EDITOR, MAINTAINER})
MAY_PROPOSE = frozenset({EDITOR, MAINTAINER})
MAY_REVIEW = frozenset({MAINTAINER})
#: Retiring a plan hides it for everyone who honours the claim, so it sits
#: with the decision-makers rather than with the editors.
MAY_RETIRE = frozenset({MAINTAINER})
_MAY_AUTHOR = MAY_PROPOSE
_MAY_DECIDE = MAY_REVIEW


@dataclass(frozen=True)
class Event:
    """A signed envelope together with the payload it commits to."""

    artifact: Artifact
    payload: dict[str, Any]

    @property
    def event_id(self) -> str:
        return self.artifact.artifact_id

    @property
    def actor(self) -> str:
        """Who acted: the user when known, else the device that signed."""
        return self.artifact.actor_user_id or self.artifact.actor_device_id


def _sign_event(
    artifact_type: str,
    workspace_id: str,
    payload: dict[str, Any],
    *,
    plan_file_id: str | None = None,
    parents: tuple[str, ...] | list[str] = (),
    actor_user_id: str | None = None,
    **kw: Any,
) -> Event:
    artifact = artifacts.make_artifact(
        artifact_type=artifact_type,
        workspace_id=workspace_id,
        content_hash=artifacts.hash_bytes(artifacts.canonical_bytes(payload)),
        plan_file_id=plan_file_id,
        parents=parents,
        actor_user_id=actor_user_id,
        **kw,
    )
    return Event(artifact=artifact, payload=payload)


def make_comment(
    *,
    workspace_id: str,
    plan_file_id: str,
    target_artifact_id: str,
    quote: str,
    body: str,
    occurrence: int = 0,
    target_version: int | None = None,
    actor_user_id: str | None = None,
    **kw: Any,
) -> Event:
    """A note attached to a quotation in a plan.

    Anchored by what it quotes, never by a line or an offset: the plan will
    be edited, and a note that had silently slid onto a different paragraph
    would be worse than one that admits it lost its place.

    A comment is a signed artifact like everything else, so it syncs to
    peers, verifies offline, and survives being written on a train.
    """
    payload = {
        "target_artifact_id": target_artifact_id,
        "target_version": target_version,
        "anchor": {"quote": quote, "occurrence": occurrence},
        "body": body,
    }
    return _sign_event(
        artifacts.COMMENT,
        workspace_id,
        payload,
        plan_file_id=plan_file_id,
        actor_user_id=actor_user_id,
        **kw,
    )


def make_tombstone(
    *,
    workspace_id: str,
    plan_file_id: str,
    reason: str = "",
    restored: bool = False,
    actor_user_id: str | None = None,
    **kw: Any,
) -> Event:
    """Ask peers to stop showing a plan, or to start showing it again.

    Deliberately not called a deletion. Nothing is erased: the artifacts
    remain, their signatures still verify, and a device that was offline
    when this was signed holds the content either way. What this carries is
    a claim, and every honest thing the UI says about it has to be phrased
    that way.

    Reversal is another tombstone with ``restored`` set, rather than a
    second artifact type or a mutation of the first. Append-only stores do
    not take things back; they record what happened next.
    """
    payload = {"reason": reason.strip(), "restored": restored}
    return _sign_event(
        artifacts.PLAN_TOMBSTONE,
        workspace_id,
        payload,
        plan_file_id=plan_file_id,
        actor_user_id=actor_user_id,
        **kw,
    )


#: Notes from somebody with no device key, vouched for by the device that
#: imported them. Kept a distinct type so nothing can mistake it for a
#: teammate's signed comment.
EXTERNAL_REVIEW = artifacts.REVIEW_EXTERNAL


def make_external_review(
    *,
    workspace_id: str,
    plan_file_id: str,
    target_artifact_id: str,
    reviewer: str,
    notes: list[dict[str, Any]],
    target_version: int | None = None,
    source: str = "packet",
    actor_user_id: str | None = None,
    **kw: Any,
) -> Event:
    """Record review from outside the mesh.

    The reviewer has no key, so this event is signed by the device doing the
    importing. That signature is a claim about provenance — "I received this
    from someone calling themselves X" — and never about identity. The
    payload says so in a field rather than in a comment, because consumers
    have to be able to tell the two apart without reading this docstring.
    """
    payload = {
        "target_artifact_id": target_artifact_id,
        "target_version": target_version,
        "reviewer": reviewer,
        "verified": False,
        "source": source,
        "notes": notes,
    }
    return _sign_event(
        EXTERNAL_REVIEW,
        workspace_id,
        payload,
        plan_file_id=plan_file_id,
        actor_user_id=actor_user_id,
        **kw,
    )


def make_proposal(
    *,
    workspace_id: str,
    plan_file_id: str,
    target_artifact_id: str,
    base_accepted_event_ids: tuple[str, ...] | list[str] = (),
    message: str = "",
    policy: Policy = DEFAULT_POLICY,
    actor_user_id: str | None = None,
    **kw: Any,
) -> Event:
    """Offer an exact version for review."""
    payload = {
        "target_artifact_id": target_artifact_id,
        "base_accepted_event_ids": sorted(base_accepted_event_ids),
        "message": message,
        "policy_id": policy.policy_id,
    }
    return _sign_event(
        artifacts.REVIEW_PROPOSAL,
        workspace_id,
        payload,
        plan_file_id=plan_file_id,
        actor_user_id=actor_user_id,
        **kw,
    )


def make_decision(
    *,
    workspace_id: str,
    plan_file_id: str,
    proposal_id: str,
    target_artifact_id: str,
    action: str,
    actor_user_id: str | None = None,
    **kw: Any,
) -> Event:
    """Record a review decision against an exact proposal and version.

    The target is named explicitly so a decision can never be replayed
    against different content than the reviewer saw.
    """
    if action not in DECISION_ACTIONS:
        raise ValueError(f"Unknown review action: {action}")
    payload = {
        "proposal_id": proposal_id,
        "target_artifact_id": target_artifact_id,
        "action": action,
    }
    return _sign_event(
        artifacts.REVIEW_DECISION,
        workspace_id,
        payload,
        plan_file_id=plan_file_id,
        actor_user_id=actor_user_id,
        **kw,
    )


def make_accepted_head(
    *,
    workspace_id: str,
    plan_file_id: str,
    target_artifact_id: str,
    proposal_id: str,
    decision_event_ids: tuple[str, ...] | list[str],
    predecessor_event_ids: tuple[str, ...] | list[str] = (),
    policy: Policy = DEFAULT_POLICY,
    actor_user_id: str | None = None,
    **kw: Any,
) -> Event:
    """Advance the team baseline, citing the decisions that justify it."""
    payload = {
        "target_artifact_id": target_artifact_id,
        "proposal_id": proposal_id,
        "decision_event_ids": sorted(decision_event_ids),
        "predecessor_event_ids": sorted(predecessor_event_ids),
        "policy_id": policy.policy_id,
    }
    return _sign_event(
        artifacts.ACCEPTED_HEAD,
        workspace_id,
        payload,
        plan_file_id=plan_file_id,
        parents=predecessor_event_ids,
        actor_user_id=actor_user_id,
        **kw,
    )


@dataclass(frozen=True)
class ProposalView:
    """A proposal as currently projected. Derived, never stored."""

    proposal_id: str
    target_artifact_id: str
    state: str
    proposer: str
    approvals: tuple[str, ...] = ()
    base_accepted_event_ids: tuple[str, ...] = ()


@dataclass
class WorkflowState:
    """What a plan looks like right now, projected from its events."""

    accepted_artifact_id: str | None = None
    accepted_event_ids: tuple[str, ...] = ()
    conflicted: bool = False
    proposals: dict[str, ProposalView] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def implementable_artifact_id(self) -> str | None:
        """What an agent may build from: the baseline, unless it is contested."""
        return None if self.conflicted else self.accepted_artifact_id


def project(
    events: list[Event],
    roles: dict[str, str],
    policy: Policy = DEFAULT_POLICY,
) -> WorkflowState:
    """Fold append-only events into the current workflow state.

    Events may arrive in any order and may be unauthorized or incomplete.
    Anything that fails policy is recorded in ``rejected`` with a reason;
    anything still missing a dependency is held in ``pending`` and projected
    later, once the referenced artifacts show up (PRD §12.5.5).
    """
    state = WorkflowState()
    by_type: dict[str, list[Event]] = {}
    for event in events:
        by_type.setdefault(event.artifact.artifact_type, []).append(event)

    proposals: dict[str, Event] = {}
    for event in by_type.get(artifacts.REVIEW_PROPOSAL, []):
        if roles.get(event.actor) not in _MAY_AUTHOR:
            state.rejected.append((event.event_id, f"{event.actor} may not propose"))
            continue
        proposals[event.event_id] = event

    decisions: dict[str, list[Event]] = {}
    for event in by_type.get(artifacts.REVIEW_DECISION, []):
        proposal_id = str(event.payload.get("proposal_id", ""))
        proposal = proposals.get(proposal_id)
        if proposal is None:
            state.pending.append(event.event_id)  # proposal not here yet
            continue
        if event.payload.get("target_artifact_id") != proposal.payload.get("target_artifact_id"):
            state.rejected.append((event.event_id, "decision targets different content"))
            continue
        action = event.payload.get("action")
        role = roles.get(event.actor)
        if action == WITHDRAW:
            if event.actor != proposal.actor:
                state.rejected.append((event.event_id, "only the proposer may withdraw"))
                continue
        elif role not in _MAY_DECIDE:
            state.rejected.append((event.event_id, f"{event.actor} may not review"))
            continue
        elif (
            action == APPROVE and not policy.allow_self_approval and event.actor == proposal.actor
        ):
            state.rejected.append((event.event_id, "policy requires independent review"))
            continue
        decisions.setdefault(proposal_id, []).append(event)

    accepted = _project_accepted_heads(by_type, proposals, decisions, roles, policy, state)
    _project_proposals(proposals, decisions, accepted, state)
    return state


def _project_accepted_heads(
    by_type: dict[str, list[Event]],
    proposals: dict[str, Event],
    decisions: dict[str, list[Event]],
    roles: dict[str, str],
    policy: Policy,
    state: WorkflowState,
) -> dict[str, Event]:
    """Find the baseline, or report that two of them are contesting it."""
    valid: dict[str, Event] = {}
    for event in by_type.get(artifacts.ACCEPTED_HEAD, []):
        if roles.get(event.actor) not in _MAY_DECIDE:
            state.rejected.append((event.event_id, f"{event.actor} may not accept"))
            continue
        proposal_id = str(event.payload.get("proposal_id", ""))
        if proposal_id not in proposals:
            state.pending.append(event.event_id)
            continue
        cited = set(event.payload.get("decision_event_ids") or ())
        approvals = [
            d
            for d in decisions.get(proposal_id, [])
            if d.payload.get("action") == APPROVE and d.event_id in cited
        ]
        # Distinct people. Counting cited events let one maintainer approve
        # twice and satisfy a two-approval policy alone.
        if len({d.actor for d in approvals}) < policy.approvals_required:
            state.rejected.append(
                (event.event_id, "cited approvals do not satisfy the workspace policy")
            )
            continue
        valid[event.event_id] = event

    if not valid:
        return valid

    # The baseline is the maximal accepted-head event. Two maximal events mean
    # two peers accepted without seeing each other: a conflict, not a race to
    # be settled by clock or arrival order.
    graph = {
        event_id: tuple(event.payload.get("predecessor_event_ids") or ())
        for event_id, event in valid.items()
    }
    heads = artifacts.find_heads(graph)
    state.accepted_event_ids = tuple(sorted(heads))
    if len(heads) > 1:
        state.conflicted = True
        state.accepted_artifact_id = None
    else:
        winner = valid[next(iter(heads))]
        state.accepted_artifact_id = str(winner.payload.get("target_artifact_id"))
    return valid


def _proposal_state(
    *,
    target: str,
    actions: set[Any],
    base: tuple[str, ...],
    accepted_targets: set[str],
    state: WorkflowState,
) -> str:
    """Which state a proposal is in, by the first rule that applies.

    The order is the precedence, and it is the whole content of this
    function. Withdrawal and rejection outrank everything, because both are
    the proposer or a reviewer ending the discussion. An accepted target
    outranks a change request, because the change was evidently made. Being
    stale is checked last, since it only matters for a proposal still open.
    """
    if WITHDRAW in actions:
        return WITHDRAWN
    if REJECT in actions:
        return REJECTED
    if target in accepted_targets:
        return ACCEPTED if target == state.accepted_artifact_id else SUPERSEDED
    if REQUEST_CHANGES in actions:
        return CHANGES_REQUESTED
    if _is_stale(base, state):
        return STALE
    return OPEN


def _project_proposals(
    proposals: dict[str, Event],
    decisions: dict[str, list[Event]],
    accepted: dict[str, Event],
    state: WorkflowState,
) -> None:
    accepted_targets = {str(e.payload.get("target_artifact_id")) for e in accepted.values()}
    for proposal_id, proposal in proposals.items():
        target = str(proposal.payload.get("target_artifact_id"))
        actions = {d.payload.get("action") for d in decisions.get(proposal_id, [])}
        # A set before sorting: the same person approving twice is one
        # approval, and `_try_accept` counts this tuple against the policy.
        approvals = tuple(
            sorted(
                {
                    d.actor
                    for d in decisions.get(proposal_id, [])
                    if d.payload.get("action") == APPROVE
                }
            )
        )
        base = tuple(proposal.payload.get("base_accepted_event_ids") or ())

        projected = _proposal_state(
            target=target,
            actions=actions,
            base=base,
            accepted_targets=accepted_targets,
            state=state,
        )

        state.proposals[proposal_id] = ProposalView(
            proposal_id=proposal_id,
            target_artifact_id=target,
            state=projected,
            proposer=proposal.actor,
            approvals=approvals,
            base_accepted_event_ids=base,
        )


def _is_stale(base: tuple[str, ...], state: WorkflowState) -> bool:
    """An open proposal is stale once the baseline has moved past its base.

    Its content and review history stay valid; it simply cannot advance the
    baseline until it is rebased or resubmitted (PRD §12.5.4).
    """
    if not state.accepted_event_ids:
        return False
    return set(base) != set(state.accepted_event_ids)
