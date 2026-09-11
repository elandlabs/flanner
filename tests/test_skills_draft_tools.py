"""What an agent may do with Skills: hand over evidence, draft and revise.

Each of these creates something for a person to review and nothing else.
The evidence is labelled as the agent's own account, a draft is never
approved by the call that made it, and revising an approved draft leaves
the approval behind on the text that was read.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from flanner import actions, skills_learn
from flanner.database import SkillApprovalModel, create_project, get_session
from flanner.services import dispatch

STEPS = "1. Run the release script.\n2. Tag the commit.\n3. Post the notes."
DRAFT = "---\nname: cut-a-release\ndescription: Cut a release\n---\n" + STEPS


@pytest.fixture
def project(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    monkeypatch.chdir(root)
    session = get_session()
    proj = create_project(session, name="p", project_root=str(root), auto_gitignore=False)
    return session, proj


def as_agent(op: str, **args):
    return dispatch(op, args, surface=actions.AGENT)


def test_evidence_from_an_agent_is_labelled_as_its_own_account(project):
    session, proj = project
    filed = as_agent("skills_submit_evidence", summary="Cut a release", body=STEPS)

    assert "error" not in filed, filed
    assert [row.source for row in skills_learn.evidence(session, proj)] == [skills_learn.BY_AGENT]


def test_evidence_carrying_a_credential_is_refused(project):
    refused = as_agent(
        "skills_submit_evidence",
        summary="Deploy",
        body="Paste this in:\n-----BEGIN RSA PRIVATE KEY-----\n",
    )

    assert refused.get("error") is True, refused


def test_a_draft_from_an_agent_is_a_draft_and_nothing_more(project):
    session, _ = project
    evidence_id = as_agent("skills_submit_evidence", summary="Cut a release", body=STEPS)["id"]

    drafted = as_agent(
        "skills_propose", skill_name="cut-a-release", body=DRAFT, provenance=[evidence_id]
    )

    assert drafted["state"] == skills_learn.DRAFT
    assert session.query(SkillApprovalModel).count() == 0
    assert skills_learn.authorized(session, drafted["id"]) == (False, "nobody has approved this")


def test_a_draft_that_names_no_evidence_is_refused(project):
    refused = as_agent("skills_propose", skill_name="cut-a-release", body=DRAFT, provenance=[])

    assert refused.get("error") is True, refused


def test_revising_an_approved_draft_needs_a_fresh_review(project):
    session, _ = project
    evidence_id = as_agent("skills_submit_evidence", summary="Cut a release", body=STEPS)["id"]
    drafted = as_agent(
        "skills_propose", skill_name="cut-a-release", body=DRAFT, provenance=[evidence_id]
    )
    skills_learn.decide(session, drafted["id"], skills_learn.APPROVED, actor="a person")

    revised = as_agent("skills_revise", proposal_id=drafted["id"], body=DRAFT + "\n4. Celebrate.")

    assert revised["state"] == skills_learn.DRAFT
    session.expire_all()
    allowed, why = skills_learn.authorized(session, drafted["id"])
    assert not allowed and "changed after it was approved" in why


def test_each_draft_tool_is_recorded_as_the_agents(project):
    session, _ = project
    filed = as_agent("skills_submit_evidence", summary="Cut a release", body=STEPS)

    row = actions.view(actions.get(session, filed["action_id"]))

    assert (row["surface"], row["operation"]) == ("agent", "skills_submit_evidence")
    assert STEPS not in str(row), "the history keeps ids, never the work itself"


def test_no_tool_lets_an_agent_approve_or_install_a_skill():
    from flanner import server

    names = {tool.name for tool in asyncio.run(server._mcp.list_tools())}

    assert {"skills_submit_evidence", "skills_propose", "skills_revise"} <= names
    assert not {name for name in names if "approve" in name or name == "skills_install"}
