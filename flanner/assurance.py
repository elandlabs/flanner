"""Is this plan safe to implement, and what exactly would I be implementing?

Phase 5's whole point, in one answer (PRD §25, Phase 5 exit criterion): an
agent should be able to state the exact artifact it is building from, the
code revision that artifact was written against, the freshness evidence for
it, and whether anyone approved it.

Those four facts already exist in separate places. This module joins them
and then applies the workspace policy, so the decision to proceed is made
once, consistently, rather than re-derived by each caller from parts they
might read differently.

Two rules are not policy-configurable, because getting them wrong is a
correctness failure rather than a preference:

- A contested baseline always blocks. Two peers accepted different versions
  while disconnected, so there is no single answer to "the current plan"
  and an agent must stop rather than pick one (§20).
- Evidence always names its evaluator. Freshness is computed against one
  checkout on one device, so a record that does not say which is not
  evidence, it is an opinion (§13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import artifacts, authz, identity, workflow
from .artifacts import Artifact
from .database import (
    ArtifactModel,
    PlanFileModel,
    ProjectModel,
    get_version,
    list_artifacts,
)
from .freshness import compute_freshness
from .frontmatter import read_managed
from .workflow import BLOCK, Policy

# Freshness statuses that mean the plan may no longer describe the code.
_DOUBTFUL = frozenset({"suspect", "stale"})

_REVIEW_TYPES = (
    artifacts.REVIEW_PROPOSAL,
    artifacts.REVIEW_DECISION,
    artifacts.ACCEPTED_HEAD,
)


@dataclass(frozen=True)
class Assurance:
    """Everything an agent needs to decide whether to act, and to cite why."""

    plan_name: str
    artifact_id: str | None = None
    version: int | None = None
    commit_anchor: str | None = None
    freshness_status: str = "unknown"
    freshness_reasons: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    accepted_artifact_id: str | None = None
    reviewed: bool = False
    conflicted: bool = False
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # Which regime produced this verdict: "local" means review gated
    # nothing, "entitlement" means a signed capability decided it.
    authorization: str = authz.LOCAL

    @property
    def safe_to_implement(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_name": self.plan_name,
            "artifact_id": self.artifact_id,
            "version": self.version,
            "commit_anchor": self.commit_anchor,
            "freshness_status": self.freshness_status,
            "freshness_reasons": list(self.freshness_reasons),
            "evidence": self.evidence,
            "accepted_artifact_id": self.accepted_artifact_id,
            "reviewed": self.reviewed,
            "conflicted": self.conflicted,
            "safe_to_implement": self.safe_to_implement,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "authorization": self.authorization,
        }


@dataclass(frozen=True)
class Retirement:
    """Whether a plan has been asked to disappear, and who asked."""

    retired: bool
    by: str = ""
    reason: str = ""
    at: str = ""

    @property
    def claimed(self) -> bool:
        """True when somebody has spoken about this plan either way."""
        return bool(self.at)


def retirement(session: Session, plan_file_id: str) -> Retirement:
    """The current standing of a plan's tombstones.

    Latest wins, ordered by the envelope timestamp with the artifact id as
    the tiebreaker. That is a weaker rule than the head resolution used for
    plan content, and deliberately: two maintainers disagreeing about
    whether a plan should be visible is not a merge conflict anybody can
    usefully be shown, and leaving it `conflicted` would mean showing the
    plan to some people and not others with no way to settle it. Last word
    wins, and the whole exchange stays in the history.

    A device that never receives the tombstone keeps showing the plan.
    Nothing here can prevent that, which is exactly why this is called a
    claim rather than a deletion.
    """
    events = _load_typed(session, plan_file_id, artifacts.PLAN_TOMBSTONE)
    if not events:
        return Retirement(retired=False)
    latest = max(events, key=lambda e: (e.artifact.created_at, e.artifact.artifact_id))
    return Retirement(
        retired=not bool(latest.payload.get("restored")),
        by=str(latest.artifact.actor_user_id or latest.artifact.actor_device_id or ""),
        reason=str(latest.payload.get("reason") or ""),
        at=str(latest.artifact.created_at or ""),
    )


def retired_plan_ids(session: Session) -> set[str]:
    """Every plan currently claimed as retired, for filtering a listing.

    One pass over the tombstones rather than a projection per plan, because
    the callers are list pages that would otherwise do this per row.
    """
    by_plan: dict[str, tuple[str, str, bool]] = {}
    for row in list_artifacts(session, artifact_type=artifacts.PLAN_TOMBSTONE):
        if row.plan_file_id is None or row.payload is None:
            continue
        try:
            payload = json.loads(row.payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        key = (str(row.created_at), str(row.artifact_id))
        seen = by_plan.get(row.plan_file_id)
        if seen is None or key > (seen[0], seen[1]):
            by_plan[row.plan_file_id] = (key[0], key[1], not bool(payload.get("restored")))
    return {plan for plan, (_, _, retired) in by_plan.items() if retired}


def count_review_subjects(session: Session) -> int:
    """How many plans have anything on the Review page.

    One distinct query rather than projecting review state for every plan,
    because this runs on every page render to draw a sidebar badge. It
    counts subjects, not open questions: a plan with a settled proposal and
    a comment counts once, which is also how the page lists it.

    Here rather than in `database` because it names artifact types, and the
    database layer sits below the module that defines them.
    """
    return (
        session.query(ArtifactModel.plan_file_id)
        .filter(
            ArtifactModel.plan_file_id.isnot(None),
            ArtifactModel.artifact_type.in_(
                (artifacts.COMMENT, artifacts.REVIEW_PROPOSAL, artifacts.REVIEW_EXTERNAL)
            ),
        )
        .distinct()
        .count()
    )


def load_comments(session: Session, plan_file_id: str) -> list[workflow.Event]:
    """Notes teammates left on a plan, oldest first.

    Read apart from `load_review_events` for the same reason outside review
    is: those events decide a plan's accepted baseline, and an opinion about
    a paragraph must not move it.
    """
    return _load_typed(session, plan_file_id, artifacts.COMMENT)


def load_external_reviews(session: Session, plan_file_id: str) -> list[workflow.Event]:
    """Notes imported from outside the mesh.

    Deliberately not part of ``load_review_events``. Those feed the
    projection that decides a plan's accepted baseline, and an outsider's
    unsigned note must never move that. This is commentary; it is read
    separately and shown separately.
    """
    return _load_typed(session, plan_file_id, artifacts.REVIEW_EXTERNAL)


def _load_typed(session: Session, plan_file_id: str, wanted: str) -> list[workflow.Event]:
    """Every artifact of one type, with its payload, oldest first."""
    events: list[workflow.Event] = []
    for row in list_artifacts(session, plan_file_id=plan_file_id):
        if row.artifact_type != wanted or row.payload is None:
            continue
        try:
            payload = json.loads(row.payload)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        events.append(
            workflow.Event(
                artifact=Artifact(
                    artifact_type=row.artifact_type,
                    workspace_id=row.workspace_id,
                    content_hash=row.content_hash,
                    actor_device_id=row.actor_device_id,
                    created_at=row.created_at,
                    artifact_id=row.artifact_id,
                    signature=row.signature,
                    protocol_version=row.protocol_version,
                    organization_id=row.organization_id,
                    plan_file_id=row.plan_file_id,
                    actor_user_id=row.actor_user_id,
                    parents=(),
                ),
                payload=payload,
            )
        )
    return events


def load_review_events(session: Session, plan_file_id: str) -> list[workflow.Event]:
    """Rebuild a plan's review events from the artifacts that carry them.

    Events are stored like any other artifact, with their payload alongside.
    A payload that will not parse is skipped rather than raising: one bad
    row must not make a plan unreadable.
    """
    events: list[workflow.Event] = []
    for row in list_artifacts(session, plan_file_id=plan_file_id):
        if row.artifact_type not in _REVIEW_TYPES or row.payload is None:
            continue
        try:
            payload = json.loads(row.payload)
            parents = tuple(json.loads(row.parents))
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        events.append(
            workflow.Event(
                artifact=Artifact(
                    artifact_type=row.artifact_type,
                    workspace_id=row.workspace_id,
                    content_hash=row.content_hash,
                    actor_device_id=row.actor_device_id,
                    created_at=row.created_at,
                    artifact_id=row.artifact_id,
                    signature=row.signature,
                    protocol_version=row.protocol_version,
                    organization_id=row.organization_id,
                    plan_file_id=row.plan_file_id,
                    actor_user_id=row.actor_user_id,
                    parents=parents,
                ),
                payload=payload,
            )
        )
    return events


def _judge(
    *,
    state: Any,
    freshness: tuple[dict[str, Any], str, list[str]],
    reviewed: bool,
    asked_roles: bool,
    authorization: Any,
    policy: Policy,
) -> tuple[list[str], list[str]]:
    """Everything wrong with this plan, split into what blocks and what warns.

    Which side a finding lands on is policy, except for a contested baseline:
    two acceptances have no single answer, so that one is not configurable
    (§20).

    Keyword-only because eight of these are booleans and strings that would
    be silently swappable positionally.
    """
    evidence, status, reasons = freshness
    blockers: list[str] = []
    warnings: list[str] = []

    if state.conflicted:
        blockers.append("the accepted baseline is contested; two acceptances must be merged first")

    if status in _DOUBTFUL:
        detail = f"plan is {status}: " + "; ".join(reasons)
        (blockers if policy.stale_plans == BLOCK else warnings).append(detail)

    if not reviewed:
        detail = "no approval recorded for this plan"
        (blockers if policy.unreviewed_plans == BLOCK else warnings).append(detail)

    if not evidence.get("git_available"):
        warnings.append("no git repository, so freshness was judged on age alone")

    # Say so when a joined project cannot check authorization at all.
    # Silence would read as "nobody approved this" when the truth is
    # "this device cannot currently tell".
    if not asked_roles and authorization.enforced and not authorization.roles:
        warnings.append(f"review authorization is unavailable: {authorization.reason}")

    # The mirror of the case above, and the one that misleads by looking
    # settled. A solo project projects review against a role map anyone
    # holding the machine can edit, so `reviewed` is true and authorizes
    # nobody. An agent reading only `reviewed` would cite it as sign-off.
    # Exclusive with the "no approval recorded" warning by construction:
    # that one fires when `reviewed` is false, this one when it is true.
    if not asked_roles and reviewed and not authorization.enforced:
        warnings.append(
            "this approval was recorded under local roles, which anyone holding "
            "this machine can edit, so it authorizes nothing"
        )

    return blockers, warnings


def assess(
    session: Session,
    *,
    project: ProjectModel,
    plan_file: PlanFileModel,
    roles: dict[str, str] | None = None,
    policy: Policy | None = None,
) -> Assurance:
    """Judge one plan against its code and its review history."""
    version = get_version(session, plan_file.id, None)
    if version is None:
        return Assurance(
            plan_name=plan_file.name,
            blockers=("this plan has no versions",),
        )

    # Resolved through the same seam the review surface uses. If the two
    # disagreed, an approval recorded through one would look unauthorized
    # to the other, and the verdict would silently contradict the review
    # status. Note `roles or ...` would be wrong: the local placeholder is
    # an empty mapping that answers for every actor.
    authorization = authz.resolve(project)
    policy = policy or authorization.policy
    state = workflow.project(
        load_review_events(session, str(plan_file.id)),
        roles if roles is not None else authorization.roles,
        policy,
        verifier=authorization.verifier,
    )

    # Prefer the team's accepted baseline; fall back to the newest local
    # version when no review has happened, which is the normal solo case.
    artifact_id = state.accepted_artifact_id or version.artifact_id
    reviewed = state.accepted_artifact_id is not None

    evidence, status, reasons, anchor = _freshness_for(project, version)
    blockers, warnings = _judge(
        state=state,
        freshness=(evidence, status, reasons),
        reviewed=reviewed,
        asked_roles=roles is not None,
        authorization=authorization,
        policy=policy,
    )

    return Assurance(
        plan_name=plan_file.name,
        artifact_id=artifact_id,
        version=version.version,
        commit_anchor=anchor,
        freshness_status=status,
        freshness_reasons=tuple(reasons),
        evidence=evidence,
        accepted_artifact_id=state.accepted_artifact_id,
        reviewed=reviewed,
        conflicted=state.conflicted,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        authorization=authorization.source,
    )


def _freshness_for(
    project: ProjectModel, version: Any
) -> tuple[dict[str, Any], str, list[str], str | None]:
    """Freshness evidence for a version, stamped with who evaluated it."""
    root = project.project_root
    if not root:
        return ({}, "unknown", ["project has no project_root configured"], None)
    try:
        _, body = read_managed(Path(version.file_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return ({}, "unknown", [f"could not read the plan file: {e}"], None)

    evidence = compute_freshness(root, body, version.created_at)
    # Evidence is a claim by one device about one checkout, and is only
    # meaningful alongside who made it (§13).
    evidence["evaluated_by_device"] = identity.device_id()
    evidence["artifact_id"] = version.artifact_id
    return (
        evidence,
        str(evidence.get("status", "unknown")),
        list(evidence.get("reasons", [])),
        evidence.get("anchored_at_commit"),
    )
