"""Who may act in this project, and on whose authority (PRD §11.3).

One question, answered in one place, because the review surface and the
assurance verdict must not disagree about it. If they did, an approval
recorded through one would look unauthorized to the other.

There are two regimes and the difference is visible in the answer:

**Solo.** No entitlement, or a project that has not joined a workspace.
Roles come from ``workflow.local_roles()``, which answers maintainer for
everyone. Review still runs, but it gates nothing: it is a rehearsal of the
workflow, not an authorization check, and pretending otherwise would be
worse than admitting it.

**Joined.** The project names a control-plane workspace and this device
holds a usable entitlement. Roles come from the signed capability, which
cannot be self-assigned.

The failure direction matters more than either regime. Once a project has
joined a workspace, a missing, expired, or unverifiable entitlement yields
an empty role map, never the local placeholder. Falling back would mean an
expired entitlement granted strictly more than a valid one, which is the
one way this could be worse than having no authorization at all.

Nothing here reaches the network. It reads the cached entitlement through
``session``, which has no HTTP in it, so a read command stays a read.
Renewal is ``account``'s job and belongs to commands that expect to wait.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import session as cache
from . import workflow
from .database import ProjectModel
from .entitlements import approval_matches, roles_from_entitlement, verify_roster
from .plan_ops import workspace_id_for

# Where a role map came from. Reported rather than inferred, so a caller can
# tell an advisory verdict from an enforced one.
LOCAL = "local"
ENTITLEMENT = "entitlement"

#: What to do about a roster in grace. One sentence, so the review status,
#: the web page and the assurance verdict cannot word the fix differently.
RENEW_TO_COUNT_APPROVALS = "Renew to count approvals: flanner whoami --refresh"


@dataclass(frozen=True)
class Authorization:
    """The role map to project with, and the identity acting under it."""

    roles: dict[str, str]
    actor: str
    source: str
    workspace_id: str
    reason: str = ""
    #: How events are checked beyond their signatures. None where nothing
    #: is enforced, or where there is nothing to check against.
    verifier: workflow.Verifier | None = field(default=None, compare=False)
    #: True when the roster is past its expiry but inside the grace window.
    #: It still says who is who, but not who may approve.
    roster_in_grace: bool = False

    @property
    def enforced(self) -> bool:
        """True when refusals here mean something the user cannot overrule."""
        return self.source == ENTITLEMENT

    @property
    def role(self) -> str | None:
        """The acting person's own role. The map holds teammates' roles too."""
        return self.roles.get(self.actor)

    @property
    def policy(self) -> workflow.Policy:
        return workflow.TEAM_POLICY if self.enforced else workflow.DEFAULT_POLICY


def resolve(
    project: ProjectModel, *, actor: str | None = None, now: datetime | None = None
) -> Authorization:
    """Work out the authorization in force for this project."""
    workspace_id = workspace_id_for(project)

    if not project.workspace_id:
        # Solo. A local workspace id can appear in no entitlement, so there
        # is nothing to check even if this device holds one.
        return Authorization(
            roles=workflow.local_roles(),
            actor=actor or workflow.LOCAL_ACTOR,
            source=LOCAL,
            workspace_id=workspace_id,
            reason="this project has not joined a workspace",
        )

    session = cache.load()
    if session is None:
        return _refused(actor, workspace_id, "this device is not logged in")

    verdict = session.status(now=now)
    if not verdict.usable or verdict.claims is None:
        return _refused(
            actor, workspace_id, verdict.reason or f"the entitlement is {verdict.status}"
        )

    # The identity is the one the signed entitlement names, never a name a
    # caller passes in. Honouring an explicit actor handed this user's role to
    # any string at all, so `actor="alice"` acted as alice with this user's
    # authority. A name that disagrees with the signed-in user is refused
    # rather than quietly replaced, so the caller learns why.
    signed_in_as = verdict.claims.user_id
    if actor and actor != signed_in_as:
        return _refused(
            actor,
            workspace_id,
            f"cannot act as {actor}: this device is signed in as {signed_in_as}",
        )
    acting_as = signed_in_as

    # Teammates come from the signed roster. Without one, every proposal
    # and approval a teammate made was judged against a map that named
    # nobody but this user, and dropped. This user's own role still comes
    # from the entitlement, which is the fresher of the two.
    roster = verify_roster(session.roster, session.keyring, now=now) if session.roster else None
    # A roster in grace still names teammates, so proposals and comments
    # project. It does not decide approvals: a maintainer removed from the
    # team would otherwise count for the whole grace window. Serving refuses
    # such a roster for the same reason (peer.py). Nothing is deleted, so a
    # renewal makes the same approvals count again.
    in_grace = (
        roster is not None
        and verify_roster(session.roster, session.keyring, now=now, grace=timedelta(0)) is None
    )
    members = roster.members(workspace_id) if roster is not None else ()
    roles = {m.user_id: m.role for m in members if m.user_id != acting_as}
    roles.update(roles_from_entitlement(verdict.claims, workspace_id, acting_as))
    devices = {device: m.user_id for m in members for device in m.devices}
    # The entitlement itself proves this device is this user's.
    devices[verdict.claims.device_id] = acting_as

    keyring = dict(session.keyring)

    def confirmed(event: workflow.Event) -> bool:
        return approval_matches(
            str(event.payload.get("confirmation") or ""),
            keyring,
            workspace_id=event.artifact.workspace_id,
            user_id=event.actor,
            device_id=event.artifact.actor_device_id,
            proposal_id=str(event.payload.get("proposal_id") or ""),
            target_artifact_id=str(event.payload.get("target_artifact_id") or ""),
        )

    return Authorization(
        roles=roles,
        actor=acting_as,
        source=ENTITLEMENT,
        workspace_id=workspace_id,
        reason="" if acting_as in roles else "you hold no role in this workspace",
        verifier=workflow.Verifier(
            devices=devices,
            confirmed=confirmed,
            uncounted=f"the roster is in grace. {RENEW_TO_COUNT_APPROVALS}" if in_grace else "",
        ),
        roster_in_grace=in_grace,
    )


def _refused(actor: str | None, workspace_id: str, reason: str) -> Authorization:
    """A joined workspace with nothing to authorize against.

    Empty, not local: a project that joined a team must not become more
    permissive because its entitlement lapsed.
    """
    return Authorization(
        roles={},
        actor=actor or workflow.LOCAL_ACTOR,
        source=ENTITLEMENT,
        workspace_id=workspace_id,
        reason=reason,
    )
