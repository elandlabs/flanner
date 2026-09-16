"""Review in a team workspace: teammates, their devices, and a person's say-so.

Three holes, each with its own tests below.

A device held only its own role, so every proposal and approval a teammate
made was judged against a map that named nobody else, and dropped. The
signed roster fixes that.

A signature says which device wrote an event, not whose it is, so a
teammate's device could approve in anybody's name. Each event now has to be
signed by a device the roster says belongs to the person it names.

Nothing on the machine can show a person approved, because an agent with a
shell runs the same commands they do. An approval where review is enforced
now carries a confirmation the console signs for a browser signed in as the
approver. And nobody approves their own proposal while somebody else could
review it.
"""

from __future__ import annotations

import base64
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import authz, identity, review, workflow
from flanner import session as cache
from flanner.artifacts import canonical_bytes
from flanner.cli import cli
from flanner.database import create_project, get_session
from flanner.entitlements import (
    APPROVAL,
    ROSTER,
    Claims,
    WorkspaceCapability,
    approval_matches,
    approval_subject,
    encode_token,
    read_signed,
    verify_roster,
)
from flanner.identity import public_key_b64, sign
from flanner.plan_ops import create_plan
from flanner.workflow import APPROVE, EDITOR, MAINTAINER

WORKSPACE = "ws_core"
ISSUER = Ed25519PrivateKey.generate()
KEYRING = {"sk_1": public_key_b64(ISSUER.public_key())}
TEAMMATE = Ed25519PrivateKey.generate()
TEAMMATE_DEVICE = identity.device_id_for(TEAMMATE.public_key())


def _stamp(offset: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat().replace("+00:00", "Z")


def signed(fields: dict) -> str:
    """A document the control plane signed, in the shape it sends."""
    data = canonical_bytes({**fields, "key_id": "sk_1"})
    return base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + sign(data, ISSUER)


def roster(*members: tuple[str, str, tuple[str, ...]], expires: timedelta = timedelta(hours=1)):
    return signed(
        {
            "kind": ROSTER,
            "organization_id": "org_1",
            "issued_at": _stamp(),
            "expires_at": _stamp(expires),
            "workspaces": {
                WORKSPACE: [
                    {"user_id": user, "role": role, "devices": list(devices)}
                    for user, role, devices in members
                ]
            },
        }
    )


def confirmation(proposal: str, target: str, *, user: str = "maria", device: str | None = None):
    return signed(
        {
            "kind": APPROVAL,
            "organization_id": "org_1",
            "workspace_id": WORKSPACE,
            "user_id": user,
            "device_id": device or identity.device_id(),
            "subject": approval_subject(proposal, target),
            "confirmed_at": _stamp(),
        }
    )


def sign_in(role: str | None = MAINTAINER, *, user: str = "maria", team: str = "") -> None:
    """Cache this device's session: its entitlement, and the roster if given."""
    device = identity.device_id()
    claims = Claims(
        organization_id="org_1",
        user_id=user,
        device_id=device,
        key_id="sk_1",
        issued_at=_stamp(-timedelta(minutes=1)),
        expires_at=_stamp(timedelta(hours=1)),
        workspace_capabilities=(WorkspaceCapability(WORKSPACE, role),) if role else (),
    )
    cache.save(
        cache.Session(
            endpoint="https://api.example.test",
            device_id=device,
            organization_id="org_1",
            user_id=user,
            entitlement=encode_token(claims, sign(canonical_bytes(claims.to_dict()), ISSUER)),
            keyring=KEYRING,
            roster=team,
        )
    )


@pytest.fixture
def joined(db, tmp_path, monkeypatch):
    """A project in the team workspace, with one plan.

    FLANNER_HOME is where `db` put the catalog, so the command line finds
    it. Not a second home: that would be a second device key, and the
    device binding under test would rightly refuse its events.
    """
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    session = get_session()
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    proj = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    plan_file, version = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="user"
    )
    proj.workspace_id = WORKSPACE
    session.commit()
    return session, proj, plan_file, version


def teammate_proposes(joined, *, user: str = "mo", key: Ed25519PrivateKey = TEAMMATE):
    session, _, plan_file, version = joined
    event = workflow.make_proposal(
        workspace_id=WORKSPACE,
        plan_file_id=str(plan_file.id),
        target_artifact_id=version.artifact_id,
        actor_user_id=user,
        signing_key=key,
    )
    review.save_event(session, event, str(plan_file.id))
    session.commit()
    return event


def approve(joined, proposal, **kw):
    session, proj, plan_file, _ = joined
    return review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal.event_id,
        action=APPROVE,
        **kw,
    )


# --- teammates -----------------------------------------------------------------


def test_a_teammates_proposal_counts_once_the_roster_names_them(joined):
    session, proj, plan_file, _ = joined
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    proposal = teammate_proposes(joined)

    state = review.status(session, plan_file=plan_file, project=proj)

    assert state.proposals[proposal.event_id].proposer == "mo"


def test_without_a_roster_a_teammates_proposal_is_still_dropped(joined):
    session, proj, plan_file, _ = joined
    sign_in()
    proposal = teammate_proposes(joined)

    state = review.status(session, plan_file=plan_file, project=proj)

    assert proposal.event_id not in state.proposals
    assert any("does not belong to mo" in why for _, why in state.rejected)


def test_a_roster_past_its_grace_window_names_nobody(joined):
    session, proj, plan_file, _ = joined
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,)), expires=-timedelta(days=8)))
    proposal = teammate_proposes(joined)

    state = review.status(session, plan_file=plan_file, project=proj)
    assert proposal.event_id not in state.proposals


def test_your_own_role_comes_from_the_entitlement_not_the_roster(joined):
    _, proj, _, _ = joined
    sign_in(EDITOR, team=roster(("maria", MAINTAINER, (identity.device_id(),))))

    assert authz.resolve(proj).role == EDITOR


# --- devices -------------------------------------------------------------------


def test_a_teammates_device_cannot_act_in_somebody_elses_name(joined):
    session, proj, plan_file, _ = joined
    sign_in(team=roster(("mo", MAINTAINER, (TEAMMATE_DEVICE,))))
    forged = teammate_proposes(joined, user="maria")

    state = review.status(session, plan_file=plan_file, project=proj)

    assert forged.event_id not in state.proposals
    assert any("does not belong to maria" in why for _, why in state.rejected)


# --- confirmation --------------------------------------------------------------


def test_an_approval_here_is_refused_without_a_console_confirmation(joined):
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    proposal = teammate_proposes(joined)

    with pytest.raises(PermissionError, match="not confirmed"):
        approve(joined, proposal)


def test_a_confirmed_approval_advances_the_baseline(joined):
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    proposal = teammate_proposes(joined)
    target = proposal.payload["target_artifact_id"]

    result = approve(joined, proposal, confirmation=confirmation(proposal.event_id, target))

    assert result.advanced_baseline, result.reason


def test_a_confirmation_does_not_carry_over_to_another_proposal(joined):
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    proposal = teammate_proposes(joined)
    target = proposal.payload["target_artifact_id"]

    with pytest.raises(PermissionError, match="not confirmed"):
        approve(joined, proposal, confirmation=confirmation("some-other-proposal", target))


def test_a_signed_document_is_only_ever_read_as_its_own_kind():
    team = roster(("mo", EDITOR, (TEAMMATE_DEVICE,)))
    agreed = confirmation("p1", "t1")

    assert read_signed(team, KEYRING, APPROVAL) is None
    assert read_signed(agreed, KEYRING, ROSTER) is None
    assert verify_roster(team[:-2] + "AA", KEYRING) is None
    assert approval_matches(
        agreed,
        KEYRING,
        workspace_id=WORKSPACE,
        user_id="maria",
        device_id=identity.device_id(),
        proposal_id="p1",
        target_artifact_id="t1",
    )
    assert not approval_matches(
        agreed,
        KEYRING,
        workspace_id=WORKSPACE,
        user_id="mo",
        device_id=identity.device_id(),
        proposal_id="p1",
        target_artifact_id="t1",
    )


# --- self-approval -------------------------------------------------------------


def test_you_cannot_approve_your_own_proposal_when_someone_else_can_review(joined):
    session, proj, plan_file, _ = joined
    sign_in(team=roster(("mo", MAINTAINER, (TEAMMATE_DEVICE,))))
    proposed = review.propose(session, project=proj, plan_file=plan_file).event
    target = proposed.payload["target_artifact_id"]

    with pytest.raises(PermissionError, match="your own proposal"):
        approve(joined, proposed, confirmation=confirmation(proposed.event_id, target))


def test_a_sole_maintainer_may_still_approve_their_own(joined):
    session, proj, plan_file, _ = joined
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    proposed = review.propose(session, project=proj, plan_file=plan_file).event
    target = proposed.payload["target_artifact_id"]

    result = approve(joined, proposed, confirmation=confirmation(proposed.event_id, target))

    assert result.advanced_baseline, result.reason


# --- the command a person runs -------------------------------------------------


def _answers(monkeypatch, *states):
    from flanner import account
    from flanner import cli as cli_module

    monkeypatch.setattr(cli_module, "APPROVAL_POLL_SECONDS", 0)
    monkeypatch.setattr(
        account,
        "request_approval",
        lambda **_: {
            "request_id": "apr_1",
            "url": "https://console.test/approve/apr_1",
            "code": "K7QD-42XM",
        },
    )
    replies = iter(states)
    monkeypatch.setattr(account, "approval_status", lambda _: next(replies))


def _approving(proposal):
    command = ["review", "decide", "arch", "approve"]
    return [*command, "--proposal", proposal.event_id, "--project", "p"]


def test_the_command_waits_for_the_console_then_records_the_approval(joined, monkeypatch):
    session, proj, plan_file, _ = joined
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    proposal = teammate_proposes(joined)
    agreed = confirmation(proposal.event_id, proposal.payload["target_artifact_id"])
    _answers(monkeypatch, {"state": "pending"}, {"state": "confirmed", "confirmation": agreed})

    ran = CliRunner().invoke(cli, _approving(proposal))

    assert ran.exit_code == 0, ran.output
    assert "https://console.test/approve/apr_1" in ran.output
    assert "K7QD-42XM" in ran.output
    assert review.status(session, plan_file=plan_file, project=proj).accepted_artifact_id


def test_declining_in_the_console_records_nothing(joined, monkeypatch):
    session, proj, plan_file, _ = joined
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    proposal = teammate_proposes(joined)
    _answers(monkeypatch, {"state": "declined"})

    ran = CliRunner().invoke(cli, _approving(proposal))

    assert ran.exit_code == 1
    assert "declined" in ran.output
    assert (
        review.status(session, plan_file=plan_file, project=proj)
        .proposals[proposal.event_id]
        .approvals
        == ()
    )


# --- a roster in grace -----------------------------------------------------------
#
# A roster past its expiry is still honoured for a grace window, so a
# maintainer removed from the team kept counting for up to a week. While the
# roster is in grace it still says who is who, but no approval counts and no
# head is accepted. Nothing is deleted: a renewal brings them back.

IN_GRACE = -timedelta(days=1)


def _accepted_then(joined, expires: timedelta):
    """A teammate's proposal accepted on a current roster, then the roster aged."""
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    proposal = teammate_proposes(joined)
    target = proposal.payload["target_artifact_id"]
    assert approve(joined, proposal, confirmation=confirmation(proposal.event_id, target)).accepted
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,)), expires=expires))
    return proposal


def test_a_maintainers_approval_does_not_count_while_the_roster_is_in_grace(joined):
    session, proj, plan_file, _ = joined
    proposal = _accepted_then(joined, IN_GRACE)

    state = review.status(session, plan_file=plan_file, project=proj)

    assert state.proposals[proposal.event_id].approvals == ()
    assert any(authz.RENEW_TO_COUNT_APPROVALS in why for _, why in state.rejected)


def test_no_head_is_accepted_while_the_roster_is_in_grace(joined):
    session, proj, plan_file, _ = joined
    proposal = _accepted_then(joined, IN_GRACE)

    state = review.status(session, plan_file=plan_file, project=proj)

    assert state.accepted_artifact_id is None
    assert state.accepted_event_ids == ()
    assert state.proposals[proposal.event_id].state != workflow.ACCEPTED


def test_the_same_approval_counts_again_once_the_roster_is_current(joined):
    session, proj, plan_file, _ = joined
    proposal = _accepted_then(joined, IN_GRACE)
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))

    state = review.status(session, plan_file=plan_file, project=proj)

    assert state.proposals[proposal.event_id].approvals == ("maria",)
    assert state.accepted_artifact_id == proposal.payload["target_artifact_id"]
    assert not authz.resolve(proj).roster_in_grace


def test_proposals_and_comments_still_show_while_the_roster_is_in_grace(joined):
    from flanner.assurance import load_comments

    session, proj, plan_file, _ = joined
    proposal = _accepted_then(joined, IN_GRACE)
    review.comment(session, project=proj, plan_file=plan_file, quote="one", body="still here")

    state = review.status(session, plan_file=plan_file, project=proj)

    assert state.proposals[proposal.event_id].proposer == "mo"
    assert [c.payload["body"] for c in load_comments(session, str(plan_file.id))] == ["still here"]


def test_an_approval_in_grace_is_recorded_but_does_not_advance(joined):
    session, proj, plan_file, _ = joined
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,)), expires=IN_GRACE))
    proposal = teammate_proposes(joined)
    target = proposal.payload["target_artifact_id"]

    result = approve(joined, proposal, confirmation=confirmation(proposal.event_id, target))

    assert not result.advanced_baseline
    assert result.reason == authz.RENEW_TO_COUNT_APPROVALS
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,))))
    state = review.status(session, plan_file=plan_file, project=proj)
    assert state.proposals[proposal.event_id].approvals == ("maria",)


def test_assurance_and_review_status_agree_while_the_roster_is_in_grace(joined):
    from flanner.assurance import assess

    session, proj, plan_file, _ = joined
    _accepted_then(joined, IN_GRACE)

    state = review.status(session, plan_file=plan_file, project=proj)
    verdict = assess(session, project=proj, plan_file=plan_file)

    assert verdict.reviewed is False
    assert verdict.accepted_artifact_id == state.accepted_artifact_id is None
    assert any(authz.RENEW_TO_COUNT_APPROVALS in w for w in verdict.warnings)


def test_review_status_says_to_renew_while_the_roster_is_in_grace(joined):
    _accepted_then(joined, IN_GRACE)

    ran = CliRunner().invoke(cli, ["review", "status", "arch", "--project", "p"])

    assert ran.exit_code == 0, ran.output
    assert "Renew to count approvals" in " ".join(ran.output.split())


def test_the_review_page_says_to_renew_while_the_roster_is_in_grace(joined):
    from flanner.web import _review_rows

    session = joined[0]
    _accepted_then(joined, IN_GRACE)

    rows = _review_rows(session)

    assert [row["renew"] for row in rows] == [authz.RENEW_TO_COUNT_APPROVALS]


def test_a_current_roster_asks_nobody_to_renew(joined):
    from flanner.web import _review_rows

    _accepted_then(joined, timedelta(hours=1))

    assert [row["renew"] for row in _review_rows(joined[0])] == [""]


def test_solo_review_is_untouched_by_the_roster(joined):
    session, proj, plan_file, _ = joined
    proj.workspace_id = None
    session.commit()
    sign_in(team=roster(("mo", EDITOR, (TEAMMATE_DEVICE,)), expires=IN_GRACE))
    proposed = review.propose(session, project=proj, plan_file=plan_file).event

    result = approve(joined, proposed)

    assert result.advanced_baseline, result.reason
    assert not authz.resolve(proj).roster_in_grace
