"""Recording review end to end: propose, decide, advance the baseline."""

import subprocess

import pytest

from flanner import review, workflow
from flanner.database import create_project, get_session, get_version
from flanner.plan_ops import create_plan, record_new_version
from flanner.workflow import (
    APPROVE,
    EDITOR,
    MAINTAINER,
    REJECT,
    REQUEST_CHANGES,
    WITHDRAW,
    Policy,
)


@pytest.fixture
def project(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    session = get_session()
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    proj = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    return session, proj


@pytest.fixture
def plan(project):
    session, proj = project
    plan_file, version = create_plan(
        session, project=proj, name="arch", content="# one\n", created_by="user"
    )
    session.commit()
    return plan_file, version


# --- the loop ---


def test_proposing_records_a_signed_event_without_moving_the_baseline(project, plan):
    session, proj = project
    plan_file, version = plan

    result = review.propose(session, project=proj, plan_file=plan_file, message="please look")

    assert result.event.payload["target_artifact_id"] == version.artifact_id
    assert result.advanced_baseline is False
    state = review.status(session, plan_file=plan_file)
    assert state.accepted_artifact_id is None  # proposing alone changes nothing
    assert state.proposals[result.event.event_id].state == "open"


def test_approving_advances_the_baseline(project, plan):
    session, proj = project
    plan_file, version = plan
    proposal = review.propose(session, project=proj, plan_file=plan_file)

    decision = review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal.event.event_id,
        action=APPROVE,
    )

    assert decision.advanced_baseline is True
    assert decision.reason == "baseline advanced"
    state = review.status(session, plan_file=plan_file)
    assert state.accepted_artifact_id == version.artifact_id
    assert state.conflicted is False


def test_the_events_survive_a_reload(project, plan):
    """Everything is persisted as artifacts, not held in memory."""
    session, proj = project
    plan_file, version = plan
    proposal = review.propose(session, project=proj, plan_file=plan_file)
    review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal.event.event_id,
        action=APPROVE,
    )
    session.expire_all()

    state = review.status(session, plan_file=plan_file)
    assert state.accepted_artifact_id == version.artifact_id


def test_assurance_sees_the_approval(project, plan):
    """The write surface feeds the verdict an agent reads."""
    from flanner import assurance

    session, proj = project
    plan_file, version = plan
    proposal = review.propose(session, project=proj, plan_file=plan_file)
    review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal.event.event_id,
        action=APPROVE,
    )

    verdict = assurance.assess(
        session, project=proj, plan_file=plan_file, roles=review.local_roles()
    )
    assert verdict.reviewed is True
    assert verdict.accepted_artifact_id == version.artifact_id
    assert not any("no approval" in w for w in verdict.warnings)


# --- decisions that must not advance anything ---


@pytest.mark.parametrize("action", [REJECT, REQUEST_CHANGES, WITHDRAW])
def test_a_non_approval_never_advances_the_baseline(project, plan, action):
    session, proj = project
    plan_file, _ = plan
    proposal = review.propose(session, project=proj, plan_file=plan_file)

    result = review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal.event.event_id,
        action=action,
    )
    assert result.advanced_baseline is False
    assert review.status(session, plan_file=plan_file).accepted_artifact_id is None


def test_an_approval_short_of_policy_records_but_does_not_accept(project, plan):
    session, proj = project
    plan_file, _ = plan
    two = Policy(policy_id="two-eyes", approvals_required=2)
    roles = {"maria": MAINTAINER, "mo": MAINTAINER}

    proposal = review.propose(
        session, project=proj, plan_file=plan_file, actor="maria", roles=roles, policy=two
    )
    first = review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal.event.event_id,
        action=APPROVE,
        actor="maria",
        roles=roles,
        policy=two,
    )
    assert first.advanced_baseline is False
    assert "1 of 2" in first.reason

    second = review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal.event.event_id,
        action=APPROVE,
        actor="mo",
        roles=roles,
        policy=two,
    )
    assert second.advanced_baseline is True


def test_a_non_maintainer_cannot_advance_the_baseline(project, plan):
    """Refused here, and still refused if the event reaches a peer anyway.

    The write surface stops early so nobody records a decision that can
    never count. Projection enforces the same rule independently, because
    events arriving over the wire never passed through this check
    (test_workflow covers that side).
    """
    session, proj = project
    plan_file, _ = plan
    roles = {"alice": EDITOR, "maria": MAINTAINER}
    proposal = review.propose(
        session, project=proj, plan_file=plan_file, actor="alice", roles=roles
    )

    with pytest.raises(PermissionError, match="review this plan"):
        review.decide(
            session,
            project=proj,
            plan_file=plan_file,
            proposal_id=proposal.event.event_id,
            action=APPROVE,
            actor="alice",
            roles=roles,
        )
    assert review.status(session, plan_file=plan_file, roles=roles).accepted_artifact_id is None


def test_deciding_on_an_unknown_proposal_is_refused(project, plan):
    session, proj = project
    plan_file, _ = plan
    with pytest.raises(ValueError, match="no proposal"):
        review.decide(
            session,
            project=proj,
            plan_file=plan_file,
            proposal_id="sha256:nonexistent",
            action=APPROVE,
        )


def test_a_plan_with_no_signed_version_cannot_be_proposed(project):
    from flanner.database import create_plan_file

    session, proj = project
    empty = create_plan_file(session, project_id=proj.id, name="empty")
    session.commit()
    with pytest.raises(ValueError, match="no signed version"):
        review.propose(session, project=proj, plan_file=empty)


# --- successive baselines ---


def test_a_second_approval_supersedes_the_first_without_conflict(project, plan):
    """The new transition cites the old one, so it descends rather than diverges."""
    session, proj = project
    plan_file, v1 = plan

    first = review.propose(session, project=proj, plan_file=plan_file)
    review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=first.event.event_id,
        action=APPROVE,
    )

    v2 = record_new_version(
        session, project=proj, plan_file=plan_file, content="# two\n", created_by="user", notes=""
    )
    second = review.propose(session, project=proj, plan_file=plan_file)
    review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=second.event.event_id,
        action=APPROVE,
    )

    state = review.status(session, plan_file=plan_file)
    assert state.accepted_artifact_id == v2.artifact_id
    assert state.conflicted is False, "citing the previous head must not look like divergence"
    assert state.proposals[first.event.event_id].state == "superseded"


# --- the surfaces ---


def test_the_mcp_tools_drive_the_whole_loop(project, plan):
    from flanner import server

    session, proj = project
    plan_file, version = plan
    pid = str(plan_file.id)

    proposed = server.propose_plan_revision_tool(pid, message="review please", actor="user")
    assert proposed["target_artifact_id"] == version.artifact_id

    status = server.get_plan_workflow_status_tool(pid)
    assert status["accepted_artifact_id"] is None
    assert status["proposals"][0]["state"] == "open"

    decided = server.record_plan_review_decision_tool(
        pid, proposed["proposal_id"], "approve", actor="local"
    )
    assert decided["advanced_baseline"] is True

    after = server.get_plan_workflow_status_tool(pid)
    assert after["accepted_artifact_id"] == version.artifact_id
    assert after["conflicted"] is False


def test_the_mcp_tools_report_bad_input(project):
    from flanner import server

    assert server.propose_plan_revision_tool("not-a-uuid")["error"] is True
    assert server.get_plan_workflow_status_tool("not-a-uuid")["error"] is True
    assert (
        server.record_plan_review_decision_tool(
            "00000000-0000-0000-0000-000000000000", "p", "approve"
        )["error"]
        is True
    )


def test_local_roles_are_advisory_and_say_so():
    """A local role map gates nothing; the docstring must not pretend it does."""
    assert review.local_roles()[workflow.LOCAL_ACTOR] == MAINTAINER
    assert "not a security boundary" in review.__doc__


def test_both_read_paths_agree_about_who_may_act(project, plan):
    """review.status and assurance.assess must default to the same roles.

    They previously disagreed: one used the permissive local placeholder,
    the other an empty map. An approval recorded through the review surface
    then looked unauthorized to assurance, so the verdict silently
    contradicted the review status a moment earlier.
    """
    from flanner import assurance

    session, proj = project
    plan_file, version = plan
    proposal = review.propose(session, project=proj, plan_file=plan_file, actor="claude")
    review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal.event.event_id,
        action=APPROVE,
        actor="someone-else",
    )

    # Neither call is told about roles; they must still agree.
    assert review.status(session, plan_file=plan_file).accepted_artifact_id == version.artifact_id
    assert assurance.assess(session, project=proj, plan_file=plan_file).reviewed is True


# --- enforcement, once a project joins a workspace ---


def a_signed_entitlement(role, *, workspace, user="maria"):
    """Cache a real entitlement for this device, granting `role` in `workspace`."""
    from datetime import datetime, timedelta, timezone

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from flanner import identity
    from flanner import session as cache
    from flanner.artifacts import canonical_bytes
    from flanner.entitlements import Claims, WorkspaceCapability, encode_token
    from flanner.identity import public_key_b64, sign

    # The id of the key that signs events here, as it is for a real device.
    # A made-up one would name a device that owns none of the events.
    device = identity.device_id()
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    claims = Claims(
        organization_id="org_1",
        user_id=user,
        device_id=device,
        key_id="sk_1",
        issued_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        workspace_capabilities=(
            (WorkspaceCapability(workspace_id=workspace, role=role),) if role else ()
        ),
    )
    cache.save(
        cache.Session(
            endpoint="https://api.example.test",
            device_id=device,
            organization_id="org_1",
            user_id=user,
            entitlement=encode_token(claims, sign(canonical_bytes(claims.to_dict()), key)),
            keyring={"sk_1": public_key_b64(key.public_key())},
        )
    )
    return key


def a_console_confirmation(key, *, workspace, proposal, target, user="maria"):
    """What the console signs once the approver agrees, for this device."""
    import base64

    from flanner import identity
    from flanner.artifacts import canonical_bytes
    from flanner.entitlements import APPROVAL, approval_subject
    from flanner.identity import sign

    data = canonical_bytes(
        {
            "kind": APPROVAL,
            "key_id": "sk_1",
            "organization_id": "org_1",
            "workspace_id": workspace,
            "user_id": user,
            "device_id": identity.device_id(),
            "subject": approval_subject(proposal, target),
            "confirmed_at": "2026-09-11T00:00:00Z",
        }
    )
    return base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + sign(data, key)


def join(session, proj, workspace="ws_core"):
    proj.workspace_id = workspace
    session.commit()
    return workspace


def test_a_reader_may_not_propose_once_the_project_has_joined(project, plan):
    """The same call that succeeds solo is refused under a real capability."""
    session, proj = project
    plan_file, _ = plan
    workspace = join(session, proj)
    a_signed_entitlement(workflow.READER, workspace=workspace)

    with pytest.raises(PermissionError):
        review.propose(session, project=proj, plan_file=plan_file)


def test_an_editor_may_propose_but_not_advance_the_baseline(project, plan):
    session, proj = project
    plan_file, _ = plan
    workspace = join(session, proj)
    a_signed_entitlement(EDITOR, workspace=workspace)

    review.propose(session, project=proj, plan_file=plan_file)
    state = review.status(session, plan_file=plan_file, project=proj)
    proposal_id = next(iter(state.proposals))

    with pytest.raises(PermissionError, match="review this plan"):
        review.decide(
            session, project=proj, plan_file=plan_file, proposal_id=proposal_id, action=APPROVE
        )


def test_a_maintainer_carries_the_proposal_all_the_way(project, plan):
    session, proj = project
    plan_file, _ = plan
    workspace = join(session, proj)
    key = a_signed_entitlement(MAINTAINER, workspace=workspace)

    proposed = review.propose(session, project=proj, plan_file=plan_file)
    result = review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposed.event.event_id,
        action=APPROVE,
        confirmation=a_console_confirmation(
            key,
            workspace=workspace,
            proposal=proposed.event.event_id,
            target=proposed.event.payload["target_artifact_id"],
        ),
    )
    assert result.accepted is not None, result.reason


def test_the_actor_recorded_is_the_one_the_entitlement_names(project, plan):
    """Not the local placeholder, or the role map would key to nobody."""
    session, proj = project
    plan_file, _ = plan
    workspace = join(session, proj)
    a_signed_entitlement(MAINTAINER, workspace=workspace, user="raj")

    result = review.propose(session, project=proj, plan_file=plan_file)
    assert result.event.artifact.actor_user_id == "raj"


def test_a_joined_project_with_no_entitlement_authorizes_nobody(project, plan):
    """Joining a team must not become more permissive when the token lapses."""
    session, proj = project
    plan_file, _ = plan
    join(session, proj)

    with pytest.raises(PermissionError):
        review.propose(session, project=proj, plan_file=plan_file)


def test_leaving_the_workspace_returns_the_project_to_advisory(project, plan):
    session, proj = project
    plan_file, _ = plan
    join(session, proj)
    proj.workspace_id = None
    session.commit()

    review.propose(session, project=proj, plan_file=plan_file)  # no longer refused


def test_assurance_reports_which_regime_judged_it(project, plan):
    from flanner import assurance

    session, proj = project
    plan_file, _ = plan
    assert assurance.assess(session, project=proj, plan_file=plan_file).authorization == "local"

    workspace = join(session, proj)
    a_signed_entitlement(MAINTAINER, workspace=workspace)
    verdict = assurance.assess(session, project=proj, plan_file=plan_file)
    assert verdict.authorization == "entitlement"


def test_assurance_says_so_when_it_cannot_check_authorization(project, plan):
    """Silence would read as "nobody approved", not "I cannot tell"."""
    from flanner import assurance

    session, proj = project
    plan_file, _ = plan
    join(session, proj)

    verdict = assurance.assess(session, project=proj, plan_file=plan_file)
    assert any("authorization is unavailable" in w for w in verdict.warnings)


def test_joining_brings_existing_plans_into_the_workspace(project, plan):
    """Found by dogfooding, then fixed rather than only warned about.

    The workspace id is inside the signed envelope, so joining cannot move
    an existing artifact. Each plan's current content is signed afresh into
    the workspace instead, so a team that adopts flanner late can still see
    each other's work.
    """

    session, proj = project
    a_signed_entitlement(MAINTAINER, workspace="ws_core")
    result = _join(proj)

    assert result.exit_code == 0, result.output
    assert "into the workspace" in result.output


def test_joining_can_be_told_to_leave_history_alone(project, plan):
    session, proj = project
    a_signed_entitlement(MAINTAINER, workspace="ws_core")
    result = _join(proj, "--no-adopt")

    assert result.exit_code == 0, result.output
    assert "stay local and will not sync" in result.output


def test_joining_a_project_with_no_plans_says_nothing_about_them(project):
    session, proj = project
    a_signed_entitlement(MAINTAINER, workspace="ws_core")
    result = _join(proj)

    assert result.exit_code == 0
    assert "into the workspace" not in result.output


def test_joining_without_access_changes_nothing(project, plan):
    """The order used to be bind, commit, re-sign every plan, then mention
    that this device holds no role there.

    So a mistyped workspace id cost a repository its plans' history: each
    one signed afresh as a root in a workspace nobody can reach, and the
    binding already committed by the time the warning printed. Now the
    check comes first and a refusal leaves the catalog exactly as it was.
    """
    from flanner.database import get_plan_file, list_artifacts

    session, proj = project
    plan_file, version = plan
    before = {a.artifact_id for a in list_artifacts(session)}

    result = _join(proj, workspace="ws_typo")

    assert result.exit_code == 1
    assert "No access to ws_typo" in result.output
    assert "Nothing was changed" in result.output
    session.expire_all()
    assert get_plan_file(session, plan_file.id).project.workspace_id is None, "it bound anyway"
    assert {a.artifact_id for a in list_artifacts(session)} == before, "plans were re-signed"


def test_joining_a_workspace_you_are_not_in_is_refused_even_when_logged_in(project, plan):
    """Logged in, with a real entitlement — for a different workspace."""
    session, proj = project
    a_signed_entitlement(MAINTAINER, workspace="ws_core")

    result = _join(proj, workspace="ws_other")

    assert result.exit_code == 1
    assert "No access to ws_other" in result.output


def _join(proj, *flags, workspace="ws_core"):
    """Run `flanner join` against the database the fixtures built.

    The CLI finds its catalog at FLANNER_HOME/data.db, which is not where
    the `db` fixture puts it, so point it there for the invocation.
    """
    from pathlib import Path

    from click.testing import CliRunner

    from flanner.cli import cli
    from flanner.database import get_db_path

    home = str(Path(get_db_path()).parent)

    # Joining now needs the cached entitlement, and the fixtures save it
    # under the autouse FLANNER_HOME rather than beside the database. Carry
    # it across so the CLI sees the same signed-in device the test set up.
    import shutil

    from flanner import session as cache

    if cache.session_path().exists():
        shutil.copy(cache.session_path(), Path(home) / cache.session_path().name)
    return CliRunner(env={"FLANNER_HOME": home}).invoke(
        cli, ["join", workspace, "--project", proj.name, *flags]
    )


# --- comments ---------------------------------------------------------------
#
# A comment is the one review artifact that does not move anything, which is
# exactly why it has to be strict about where it points. Every refusal below
# exists because the alternative is a note that silently attaches to nothing,
# or to the wrong sentence.


@pytest.fixture
def commentable(project):
    """A plan whose text is long enough to quote from."""
    session, proj = project
    plan_file, version = create_plan(
        session,
        project=proj,
        name="policy",
        content="# Policy\n\nThe retry budget is three attempts.\n",
        created_by="user",
    )
    session.commit()
    return session, proj, plan_file, version


def test_a_comment_records_the_quotation_it_points_at(commentable):
    session, proj, plan_file, version = commentable
    result = review.comment(
        session,
        project=proj,
        plan_file=plan_file,
        quote="The retry budget is three attempts",
        body="  Is three enough?  ",
    )
    payload = result.event.payload
    assert payload["anchor"]["quote"] == "The retry budget is three attempts"
    assert payload["body"] == "Is three enough?"
    assert payload["target_version"] == version.version


def test_a_comment_never_moves_the_baseline(commentable):
    session, proj, plan_file, _ = commentable
    review.comment(
        session,
        project=proj,
        plan_file=plan_file,
        quote="The retry budget is three attempts",
        body="noted",
    )
    assert review.status(session, plan_file=plan_file).accepted_artifact_id is None


def test_assurance_reads_back_the_comment(commentable):
    from flanner import assurance

    session, proj, plan_file, _ = commentable
    review.comment(
        session,
        project=proj,
        plan_file=plan_file,
        quote="The retry budget is three attempts",
        body="noted",
    )
    loaded = assurance.load_comments(session, str(plan_file.id))
    assert [e.payload["body"] for e in loaded] == ["noted"]


def test_quoting_text_that_is_not_in_the_plan_is_refused(commentable):
    """Otherwise the note is written, stored, and never shown to anybody."""
    session, proj, plan_file, _ = commentable
    with pytest.raises(ValueError, match="not in v1"):
        review.comment(
            session,
            project=proj,
            plan_file=plan_file,
            quote="a sentence nobody wrote",
            body="?",
        )


@pytest.mark.parametrize(
    "quote,body,message",
    [
        ("", "something", "quote something"),
        ("   ", "something", "quote something"),
        ("The retry budget is three attempts", "  ", "say something"),
    ],
)
def test_an_empty_comment_is_refused(commentable, quote, body, message):
    session, proj, plan_file, _ = commentable
    with pytest.raises(ValueError, match=message):
        review.comment(session, project=proj, plan_file=plan_file, quote=quote, body=body)


def test_commenting_on_a_version_that_does_not_exist_is_refused(commentable):
    session, proj, plan_file, _ = commentable
    with pytest.raises(ValueError, match="does not exist"):
        review.comment(
            session,
            project=proj,
            plan_file=plan_file,
            quote="The retry budget is three attempts",
            body="?",
            version=99,
        )


def test_a_comment_is_checked_against_the_version_it_names_not_the_newest(commentable):
    """The quotation has to be in the version being commented on. Checking
    the newest instead would refuse a fair comment on old text, and accept
    one on text the reader was never looking at."""
    session, proj, plan_file, _ = commentable
    record_new_version(
        session,
        project=proj,
        plan_file=plan_file,
        content="# Policy\n\nRetries are handled by the queue.\n",
        created_by="user",
        notes="rewrite",
    )
    session.commit()

    result = review.comment(
        session,
        project=proj,
        plan_file=plan_file,
        quote="The retry budget is three attempts",
        body="on the old text",
        version=1,
    )
    assert result.event.payload["target_version"] == 1

    with pytest.raises(ValueError, match="not in v2"):
        review.comment(
            session,
            project=proj,
            plan_file=plan_file,
            quote="The retry budget is three attempts",
            body="on the new text",
        )


def test_a_reader_cannot_comment(commentable):
    """Refused at the prompt rather than dropped later in projection, so
    somebody who cannot comment is told, not left watching for a note that
    will never appear."""
    session, proj, plan_file, _ = commentable
    with pytest.raises(PermissionError, match="comment on this plan"):
        review.comment(
            session,
            project=proj,
            plan_file=plan_file,
            quote="The retry budget is three attempts",
            body="?",
            actor="sam",
            roles={"sam": workflow.READER},
        )


@pytest.mark.parametrize("role", [workflow.COMMENTER, EDITOR, MAINTAINER])
def test_everyone_above_reader_can_comment(commentable, role):
    session, proj, plan_file, _ = commentable
    result = review.comment(
        session,
        project=proj,
        plan_file=plan_file,
        quote="The retry budget is three attempts",
        body="?",
        actor="sam",
        roles={"sam": role},
    )
    assert result.event.payload["body"] == "?"


# --- retiring a plan --------------------------------------------------------
#
# Never "deleting". Append-only means the bytes stay and a peer that was
# offline keeps them, so what is recorded is a claim other devices honour.
# These tests pin both halves: that it is honoured, and that it is honest.


def test_retiring_hides_a_plan_without_removing_anything(commentable):
    from flanner import assurance

    session, proj, plan_file, version = commentable
    before = len(assurance.load_review_events(session, str(plan_file.id)))

    review.retire(session, project=proj, plan_file=plan_file, reason="superseded")

    standing = assurance.retirement(session, str(plan_file.id))
    assert standing.retired is True
    assert standing.reason == "superseded"
    # Nothing was taken away: the version row, its file and every earlier
    # event are all still here.
    assert get_version(session, plan_file.id, version.version) is not None
    assert len(assurance.load_review_events(session, str(plan_file.id))) >= before


def test_a_plan_with_no_tombstone_is_not_retired(commentable):
    from flanner import assurance

    session, _, plan_file, _ = commentable
    standing = assurance.retirement(session, str(plan_file.id))
    assert standing.retired is False
    assert standing.claimed is False


def test_restoring_brings_it_back(commentable):
    from flanner import assurance

    session, proj, plan_file, _ = commentable
    review.retire(session, project=proj, plan_file=plan_file)
    review.retire(session, project=proj, plan_file=plan_file, restore=True)

    standing = assurance.retirement(session, str(plan_file.id))
    assert standing.retired is False
    # Reversal is another record, not an erasure of the first.
    assert standing.claimed is True


def test_the_latest_claim_wins(commentable):
    """Two maintainers disagreeing is not a merge conflict anybody could be
    usefully shown, so the last word wins and the argument stays in the
    history."""
    from flanner import assurance

    session, proj, plan_file, _ = commentable
    review.retire(session, project=proj, plan_file=plan_file)
    review.retire(session, project=proj, plan_file=plan_file, restore=True)
    review.retire(session, project=proj, plan_file=plan_file, reason="really gone")

    standing = assurance.retirement(session, str(plan_file.id))
    assert standing.retired is True
    assert standing.reason == "really gone"


def test_only_a_maintainer_may_retire(commentable):
    session, proj, plan_file, _ = commentable
    for role in (workflow.READER, workflow.COMMENTER, EDITOR):
        with pytest.raises(PermissionError, match="retire this plan"):
            review.retire(
                session,
                project=proj,
                plan_file=plan_file,
                actor="sam",
                roles={"sam": role},
            )


def test_a_maintainer_may_retire(commentable):
    from flanner import assurance

    session, proj, plan_file, _ = commentable
    review.retire(session, project=proj, plan_file=plan_file, actor="mo", roles={"mo": MAINTAINER})
    assert assurance.retirement(session, str(plan_file.id)).retired is True


def test_the_listing_helper_reports_every_retired_plan(project):
    from flanner import assurance

    session, proj = project
    kept, _ = create_plan(session, project=proj, name="kept", content="# a\n", created_by="u")
    gone, _ = create_plan(session, project=proj, name="gone", content="# b\n", created_by="u")
    session.commit()
    review.retire(session, project=proj, plan_file=gone)

    hidden = assurance.retired_plan_ids(session)
    assert str(gone.id) in hidden
    assert str(kept.id) not in hidden


# --- approvals belong to a person ----------------------------------------------


def test_one_person_approving_twice_is_one_approval(project, plan):
    """Counting decision events let a single maintainer satisfy a
    two-approval policy alone."""
    session, proj = project
    plan_file, _ = plan
    two = Policy(policy_id="two-eyes", approvals_required=2)
    roles = {"maria": MAINTAINER, "mo": MAINTAINER}
    proposal = review.propose(
        session, project=proj, plan_file=plan_file, actor="mo", roles=roles, policy=two
    )

    for _ in range(2):
        result = review.decide(
            session,
            project=proj,
            plan_file=plan_file,
            proposal_id=proposal.event.event_id,
            action=APPROVE,
            actor="maria",
            roles=roles,
            policy=two,
        )

    assert result.advanced_baseline is False
    assert "1 of 2" in result.reason
    state = review.status(session, plan_file=plan_file, roles=roles, policy=two)
    assert state.proposals[proposal.event.event_id].approvals == ("maria",)


def test_an_agent_cannot_approve_where_review_is_enforced(project, plan):
    session, proj = project
    plan_file, _ = plan
    workspace = join(session, proj)
    a_signed_entitlement(MAINTAINER, workspace=workspace)
    review.propose(session, project=proj, plan_file=plan_file)
    proposal_id = next(iter(review.status(session, plan_file=plan_file, project=proj).proposals))

    with pytest.raises(PermissionError, match="through an agent"):
        review.decide(
            session,
            project=proj,
            plan_file=plan_file,
            proposal_id=proposal_id,
            action=APPROVE,
            surface=review.AGENT,
        )

    # Asking for changes grants nothing, so an agent may still record it.
    result = review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal_id,
        action=REQUEST_CHANGES,
        surface=review.AGENT,
    )
    assert result.advanced_baseline is False


def test_an_agent_may_approve_in_solo_review_which_binds_nobody(project, plan):
    session, proj = project
    plan_file, _ = plan
    review.propose(session, project=proj, plan_file=plan_file)
    proposal_id = next(iter(review.status(session, plan_file=plan_file).proposals))

    result = review.decide(
        session,
        project=proj,
        plan_file=plan_file,
        proposal_id=proposal_id,
        action=APPROVE,
        surface=review.AGENT,
    )

    assert result.advanced_baseline is True


def test_the_mcp_decision_seam_is_the_agent_surface_and_ignores_a_named_actor(project, plan):
    """What an MCP call passes as `actor` is not anybody's authority."""
    from flanner import services

    session, proj = project
    plan_file, _ = plan
    workspace = join(session, proj)
    a_signed_entitlement(MAINTAINER, workspace=workspace)
    review.propose(session, project=proj, plan_file=plan_file)
    proposal_id = next(iter(review.status(session, plan_file=plan_file, project=proj).proposals))

    out = services.record_plan_review_decision(
        str(plan_file.id), proposal_id, "approve", actor="someone-else"
    )

    assert out.get("error") is True
    assert "through an agent" in out["message"]
