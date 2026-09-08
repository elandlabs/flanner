"""Proposing a skill, approving it, and comparing it against a baseline.

The M2 half. The invariants worth guarding are all about a person
staying in the loop: nothing is learned that was not handed over,
nothing is installable that was not approved, and an approval covers the
exact text somebody read and not a word more.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from flanner import skills_eval, skills_learn
from flanner.cli import cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "flanner-home"
    monkeypatch.setenv("FLANNER_HOME", str(home))
    monkeypatch.setenv("FLANNER_SKILLS_HOME", str(tmp_path / "user-home"))

    where = tmp_path / "repo"
    where.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=where, check=True)  # noqa: S603,S607
    monkeypatch.chdir(where)

    runner = CliRunner()
    started = runner.invoke(
        cli, ["init", "--skip-claude", "--project-root", str(where)], input="demo\n"
    )
    assert started.exit_code == 0, started.output
    return runner, where


def submit(runner, summary: str, *, session_ref: str = "s1", outcome: str = "test_passed"):
    return runner.invoke(
        cli,
        [
            "skills",
            "evidence",
            "submit",
            summary,
            "--body",
            "steps",
            "--session-ref",
            session_ref,
            "--outcome",
            outcome,
        ],
    )


def draft_file(where: Path, name: str = "draft.md", body: str | None = None) -> Path:
    path = where / name
    path.write_text(
        body
        or "---\nname: rebuild-staging\ndescription: Rebuild staging\n---\n\nDrop, restore.\n",
        encoding="utf-8",
    )
    return path


def a_proposal(runner, where: Path, **kwargs) -> str:
    submit(runner, "rebuild staging from a dump")
    made = runner.invoke(
        cli,
        [
            "skills",
            "propose",
            "rebuild-staging",
            "--file",
            str(draft_file(where, **kwargs)),
            "--session-ref",
            "s1",
        ],
    )
    assert made.exit_code == 0, made.output
    return json.loads(runner.invoke(cli, ["skills", "proposals", "--json"]).output)[0]["id"]


# --- evidence -----------------------------------------------------------------


def test_evidence_is_only_ever_handed_over(repo):
    """No harvesting: with nothing submitted there is nothing to learn from."""
    runner, _ = repo
    listing = runner.invoke(cli, ["skills", "evidence", "list"])
    assert "Nothing handed over yet" in listing.output


def test_a_credential_is_refused_rather_than_stored(repo):
    """A stored secret is not undone by deleting the row that carried it."""
    runner, _ = repo
    leaky = runner.invoke(
        cli,
        [
            "skills",
            "evidence",
            "submit",
            "deploy",
            "--body",
            "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
        ],
    )
    assert leaky.exit_code == 1
    assert "credential" in leaky.output


def test_repetition_opens_a_review_and_says_it_is_a_default(repo):
    runner, _ = repo
    submit(runner, "rebuild the staging database", session_ref="s1")
    not_yet = runner.invoke(cli, ["skills", "evidence", "list", "--json"])
    assert json.loads(not_yet.output)["clusters"][0]["eligible"] is False

    submit(runner, "rebuild the staging database", session_ref="s2")
    submit(runner, "rebuild the staging database", session_ref="s2")
    ready = json.loads(runner.invoke(cli, ["skills", "evidence", "list", "--json"]).output)
    assert ready["clusters"][0]["eligible"] is True
    # The threshold is a product default and has to read as one wherever it
    # is shown; a bare "ready" would be a finding the data cannot support.
    assert "not a finding" in ready["clusters"][0]["why"]

    shown = runner.invoke(cli, ["skills", "evidence", "list"]).output
    assert "ready to propose" in shown


def test_success_evidence_is_required_before_a_cluster_is_ready(repo):
    """ "Nobody complained" is not evidence that something worked."""
    runner, _ = repo
    for ref in ("s1", "s2", "s2"):
        submit(runner, "rebuild the staging database", session_ref=ref, outcome="none")
    listed = json.loads(runner.invoke(cli, ["skills", "evidence", "list", "--json"]).output)
    assert listed["clusters"][0]["eligible"] is False
    assert "nothing saying it worked" in listed["clusters"][0]["why"]


def test_a_repeated_fact_is_not_a_procedure(repo):
    """LRN-01: a repeated fact belongs in memory, not in a skill."""
    runner, _ = repo
    for ref in ("s1", "s2", "s2"):
        runner.invoke(
            cli,
            [
                "skills",
                "evidence",
                "submit",
                "the staging host is behind the vpn",
                "--kind",
                "memory",
                "--session-ref",
                ref,
                "--outcome",
                "user_accepted",
            ],
        )
    listed = json.loads(runner.invoke(cli, ["skills", "evidence", "list", "--json"]).output)
    assert listed["clusters"] == []


# --- proposals ----------------------------------------------------------------


def test_a_proposal_must_point_at_its_evidence(repo):
    """A draft that points nowhere is the shape an invented skill arrives in."""
    runner, where = repo
    bare = runner.invoke(
        cli, ["skills", "propose", "rebuild-staging", "--file", str(draft_file(where))]
    )
    assert bare.exit_code == 1
    assert "must name the evidence" in bare.output


def test_a_proposal_is_not_installable_until_somebody_approves(repo):
    runner, where = repo
    proposal_id = a_proposal(runner, where)

    rows = json.loads(runner.invoke(cli, ["skills", "proposals", "--json"]).output)
    assert rows[0]["installable"] is False
    assert rows[0]["why"] == "nobody has approved this"

    runner.invoke(cli, ["skills", "approve", proposal_id, "--actor", "me"])
    after = json.loads(runner.invoke(cli, ["skills", "proposals", "--json"]).output)
    assert after[0]["installable"] is True


def test_editing_after_approval_invalidates_the_approval(repo):
    """An approval covers bytes. Approving an id would let a review of one
    text authorize the installation of another."""
    runner, where = repo
    proposal_id = a_proposal(runner, where)
    runner.invoke(cli, ["skills", "approve", proposal_id, "--actor", "me"])

    changed = draft_file(
        where,
        "changed.md",
        body="---\nname: rebuild-staging\ndescription: Rebuild staging\n---\n\nAlso drop prod.\n",
    )
    revised = runner.invoke(cli, ["skills", "revise", proposal_id, "--file", str(changed)])
    assert revised.exit_code == 0, revised.output

    rows = json.loads(runner.invoke(cli, ["skills", "proposals", "--json"]).output)
    assert rows[0]["installable"] is False
    assert "changed after it was approved" in rows[0]["why"]


def test_a_rejection_is_not_an_approval(repo):
    runner, where = repo
    proposal_id = a_proposal(runner, where)
    runner.invoke(cli, ["skills", "reject", proposal_id, "--note", "too risky"])

    rows = json.loads(runner.invoke(cli, ["skills", "proposals", "--json"]).output)
    assert rows[0]["installable"] is False
    assert "rejected" in rows[0]["why"]


def test_a_review_shows_the_evidence_and_the_diff(repo):
    runner, where = repo
    proposal_id = a_proposal(runner, where)
    seen = json.loads(runner.invoke(cli, ["skills", "review", proposal_id, "--json"]).output)

    assert seen["evidence"]
    assert seen["evidence"][0]["source"] == "user"
    assert any(line.startswith("+name: rebuild-staging") for line in seen["diff"])
    assert any("not a discovered threshold" in note for note in seen["notes"])


def test_an_agents_own_account_is_labelled_as_such(repo):
    runner, where = repo
    from flanner.database import get_project_by_root, get_session, init_database

    init_database(str(Path(where) / ".." / "flanner-home" / "data.db"))
    session = get_session()
    project = get_project_by_root(session, str(where))
    kept = skills_learn.submit(
        session, project, "did the thing", "steps", source="agent", session_ref="s9"
    )
    made = skills_learn.propose(session, project, "did-the-thing", "body", provenance=[kept["id"]])
    seen = skills_learn.review(session, made["id"])

    assert seen["evidence"][0]["source"] == "agent"
    assert any("weaker evidence" in note for note in seen["notes"])


def test_expired_evidence_leaves_the_proposal_saying_so(repo):
    """A record that a decision was made on evidence now gone beats no record."""
    runner, where = repo
    from flanner.database import get_project_by_root, get_session, init_database

    init_database(str(Path(where) / ".." / "flanner-home" / "data.db"))
    session = get_session()
    project = get_project_by_root(session, str(where))
    kept = skills_learn.submit(session, project, "a thing", "steps", keep_days=0)
    made = skills_learn.propose(session, project, "a-thing", "body", provenance=[kept["id"]])

    # keep_days=0 stores no expiry at all, so age it by hand.
    from flanner.database import SkillEvidenceModel

    row = session.query(SkillEvidenceModel).one()
    row.expires_at = skills_learn.utcnow()
    session.commit()

    assert skills_learn.forget_expired(session)["deleted"] == 1
    seen = skills_learn.review(session, made["id"])
    assert seen["evidence"] == []
    assert seen["evidence_expired"] == 1


# --- comparisons --------------------------------------------------------------


@pytest.fixture
def suite(repo):
    runner, where = repo
    runner.invoke(
        cli,
        [
            "skills",
            "eval",
            "add-case",
            "staging",
            "restores",
            "--prompt",
            "Rebuild staging",
            "--rubric",
            "Row counts match",
        ],
    )
    runner.invoke(
        cli,
        [
            "skills",
            "eval",
            "add-case",
            "staging",
            "handles-missing",
            "--prompt",
            "Rebuild with no dump",
            "--rubric",
            "Fails loudly",
        ],
    )
    runner.invoke(
        cli,
        [
            "skills",
            "eval",
            "add-profile",
            "opus-in-claude-code",
            "--provider",
            "anthropic",
            "--model",
            "claude-opus-5",
            "--harness",
            "claude-code",
            "--harness-version",
            "2.1.0",
        ],
    )
    return runner, where


def test_a_cell_nobody_ran_is_not_run_rather_than_zero(suite):
    """An empty cell and a bad score are different facts."""
    runner, _ = suite
    runner.invoke(
        cli,
        [
            "skills",
            "eval",
            "record",
            "staging",
            "restores",
            "opus-in-claude-code",
            "--result",
            "passed",
            "--measured-by",
            "the suite",
        ],
    )

    grid = json.loads(runner.invoke(cli, ["skills", "eval", "matrix", "staging", "--json"]).output)
    missing = [c for c in grid["cells"] if c["case"] == "handles-missing"]
    assert missing and all(c["result"] == "not run" for c in missing)
    assert all(c["passed"] == 0 and c["trials"] == 0 for c in missing)
    assert any("never run" in note for note in grid["notes"])


def test_a_result_with_no_stated_source_is_refused(suite):
    """An unfalsifiable number in a comparison table is worse than a gap."""
    runner, _ = suite
    refused = runner.invoke(
        cli,
        [
            "skills",
            "eval",
            "record",
            "staging",
            "restores",
            "opus-in-claude-code",
            "--result",
            "passed",
        ],
    )
    assert refused.exit_code == 1
    assert "unattributed result is not evidence" in refused.output


def test_a_small_sample_says_it_is_a_small_sample(suite):
    runner, _ = suite
    runner.invoke(
        cli,
        [
            "skills",
            "eval",
            "record",
            "staging",
            "restores",
            "opus-in-claude-code",
            "--result",
            "passed",
            "--measured-by",
            "me",
        ],
    )
    grid = json.loads(runner.invoke(cli, ["skills", "eval", "matrix", "staging", "--json"]).output)
    baseline = grid["summary"]["(no skill: baseline)"]
    assert "too few to read as a rate" in baseline["reads_as"]


def test_the_model_and_the_harness_are_both_named(suite):
    """A model is not an agent, and a report that conflated them would be
    claiming something it cannot support."""
    runner, _ = suite
    runner.invoke(
        cli,
        [
            "skills",
            "eval",
            "record",
            "staging",
            "restores",
            "opus-in-claude-code",
            "--result",
            "passed",
            "--measured-by",
            "me",
        ],
    )
    grid = json.loads(runner.invoke(cli, ["skills", "eval", "matrix", "staging", "--json"]).output)
    profile = grid["profiles"][0]
    assert profile["model"] == "claude-opus-5"
    assert profile["harness"] == "claude-code"
    assert profile["harness_version"] == "2.1.0"
    assert any("not an agent" in limit for limit in grid["limits"])


def test_a_regression_is_reported_per_cell_not_averaged(suite):
    """A skill that helps on one model and hurts on another is the finding;
    an average is what hides it."""
    runner, _ = suite
    runner.invoke(
        cli,
        [
            "skills",
            "eval",
            "add-profile",
            "local-llama",
            "--provider",
            "local",
            "--model",
            "llama",
            "--harness",
            "script",
        ],
    )
    digest = "sha256:" + "a" * 64

    for case, profile, arm, result in (
        ("restores", "opus-in-claude-code", "", "failed"),
        ("restores", "opus-in-claude-code", digest, "passed"),
        ("handles-missing", "local-llama", "", "passed"),
        ("handles-missing", "local-llama", digest, "failed"),
    ):
        runner.invoke(
            cli,
            [
                "skills",
                "eval",
                "record",
                "staging",
                case,
                profile,
                "--result",
                result,
                "--measured-by",
                "the suite",
            ]
            + (["--skill-hash", arm] if arm else []),
        )

    grid = json.loads(runner.invoke(cli, ["skills", "eval", "matrix", "staging", "--json"]).output)
    assert grid["summary"][digest]["passed"] == 1

    shown = runner.invoke(cli, ["skills", "eval", "matrix", "staging"]).output
    assert "did worse than the baseline" in shown
    assert "handles-missing on local-llama" in shown


def test_changing_the_fixture_changes_its_hash():
    """Moving the goalposts produces a flattering number as easily as
    changing the question, so both go into the hash."""
    same = skills_eval.fixture_hash("task", "rubric")
    assert same == skills_eval.fixture_hash("task", "rubric")
    assert same != skills_eval.fixture_hash("task", "an easier rubric")
    assert same != skills_eval.fixture_hash("an easier task", "rubric")


# --- the local web page -------------------------------------------------------


def test_the_page_can_review_approve_and_invalidate(repo):
    """UI-01 again: the same authorization rules, through the other surface."""
    from starlette.testclient import TestClient

    from flanner.web import app

    runner, where = repo
    proposal_id = a_proposal(runner, where)
    client = TestClient(app, base_url="http://127.0.0.1:8000")

    page = client.get(f"/skills/proposals?open_id={proposal_id}")
    assert page.status_code == 200
    assert "rebuild-staging" in page.text
    assert "Approve this draft" in page.text

    approved = client.post(
        "/skills/proposals/decide",
        data={"proposal_id": proposal_id, "decision": "approved"},
        follow_redirects=False,
    )
    assert approved.status_code == 303
    rows = json.loads(runner.invoke(cli, ["skills", "proposals", "--json"]).output)
    assert rows[0]["installable"] is True

    edited = client.post(
        "/skills/proposals/revise",
        data={"proposal_id": proposal_id, "body": "---\nname: x\n---\n\nchanged\n"},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    after = json.loads(runner.invoke(cli, ["skills", "proposals", "--json"]).output)
    assert after[0]["installable"] is False
