"""Deciding what an agent offers is worth keeping.

`remember` is what a person asks for; this is what an agent suggests, and
the difference is that a suggestion gets checked. The four things the
acceptance criteria name are here: suggest mode asks before keeping,
credentials and denied sources are refused, duplicates do not repeat, and a
contradiction has to be resolved by somebody rather than settled quietly.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

from flanner import memory_ops as mem
from flanner.database import create_project, get_memory, get_session, init_database, list_memories
from flanner.exceptions import ValidationError
from flanner.memory_policy import Policy


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FLANNER_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S603,S607

    init_database(str(home / "data.db"))
    session = get_session()
    project = create_project(session, name="demo", project_root=str(repo), auto_gitignore=False)
    session.commit()
    return session, project, repo


@pytest.fixture
def suggesting() -> Policy:
    return Policy()


@pytest.fixture
def automatic() -> Policy:
    return Policy(capture_mode="auto_safe")


#: Four unrelated facts. Deliberately not "fact 1, fact 2": sentences
#: differing only by a number are what the conflict check exists to catch,
#: so numbering them would test that instead of the quota.
UNRELATED = (
    "Deploys go out on Tuesdays and never on a Friday.",
    "The staging database is wiped every Sunday night.",
    "Log retention is thirty days, then archived to cold storage.",
    "The health endpoint answers before migrations have finished.",
)


def _offer(session, project, policy, **kwargs):
    candidate = mem.Candidate(**kwargs)
    return mem.consider(session, [candidate], policy=policy, project=project)[0]


# --- suggest mode asks before keeping ----------------------------------------


def test_suggest_proposes_and_puts_nothing_in_recall(store, suggesting):
    """Acceptance criterion 7. A mode that says it asks and then keeps
    anyway is worse than one that never asked."""
    session, project, repo = store

    outcome = _offer(
        session,
        project,
        suggesting,
        content="Use advisory locks rather than Redis.",
        category="decision",
    )

    assert outcome["outcome"] == "proposed"
    assert list_memories(session, status="active") == []
    assert mem.recall(session, query="advisory locks", project_id=project.id)["memories"] == []
    assert len(mem.pending(session)) == 1


def test_a_proposal_carries_why_it_was_offered(store, suggesting):
    """Nobody checks it. It is what a person reads when deciding, and
    asking for it makes an agent consider whether to offer at all."""
    session, project, repo = store

    _offer(
        session,
        project,
        suggesting,
        content="Use advisory locks rather than Redis.",
        category="decision",
        why_durable="settles how the queue works for every future change",
    )

    assert "settles how the queue works" in mem.pending(session)[0]["why_durable"]


def test_approving_puts_it_in_recall(store, suggesting):
    session, project, repo = store
    outcome = _offer(
        session, project, suggesting, content="Use advisory locks.", category="decision"
    )

    mem.decide(session, memory_id=__import__("uuid").UUID(outcome["id"]), decision="approve")

    assert mem.recall(session, query="advisory locks", project_id=project.id)["memories"]


def test_rejecting_removes_it_entirely(store, suggesting):
    """A queue that keeps everything anybody turned down stops being a
    queue, and the file would outlive the decision against it."""
    session, project, repo = store
    outcome = _offer(
        session, project, suggesting, content="Use advisory locks.", category="decision"
    )
    from pathlib import Path
    from uuid import UUID

    path = Path(get_memory(session, UUID(outcome["id"])).file_path)

    mem.decide(session, memory_id=UUID(outcome["id"]), decision="reject")

    assert get_memory(session, UUID(outcome["id"])) is None
    assert not path.exists()
    assert mem.pending(session) == []


def test_editing_keeps_what_the_person_wrote(store, suggesting):
    session, project, repo = store
    from uuid import UUID

    outcome = _offer(
        session, project, suggesting, content="Use locks probably.", category="decision"
    )

    result = mem.decide(
        session,
        memory_id=UUID(outcome["id"]),
        decision="edit",
        content="Use SQLite advisory locks, because the tool must work offline.",
    )

    kept = get_memory(session, UUID(result["id"]))
    assert kept.status == "active"
    assert "work offline" in kept.body
    assert get_memory(session, UUID(outcome["id"])) is None


def test_deciding_on_something_that_is_not_a_proposal_is_refused(store, suggesting):
    session, project, repo = store
    memory, _ = mem.remember(session, content="Already kept.", category="fact", project=project)

    with pytest.raises(ValidationError, match="not a proposal"):
        mem.decide(session, memory_id=memory.id, decision="approve")


# --- what is refused ----------------------------------------------------------


def test_a_credential_is_refused(store, suggesting):
    """Acceptance criterion 8, on the path an agent uses."""
    session, project, repo = store

    outcome = _offer(
        session,
        project,
        suggesting,
        content="the deploy key is AKIAIOSFODNN7EXAMPLE",
        category="fact",
    )

    assert outcome["outcome"] == "rejected"
    assert outcome["reason"].startswith("secret:")
    assert list_memories(session, status=None) == []


def test_a_denied_source_is_refused(store):
    """The other half of criterion 8. Raw terminal output is not context."""
    session, project, repo = store
    policy = Policy(deny_sources=("tool_observation",))

    outcome = _offer(
        session,
        project,
        policy,
        content="The build printed 400 lines of warnings.",
        category="fact",
        source_type="tool_observation",
    )

    assert outcome["outcome"] == "rejected"
    assert outcome["reason"] == mem.REJECTED_DENIED_SOURCE


def test_a_category_this_project_does_not_keep_is_refused(store):
    session, project, repo = store
    policy = Policy(allow_categories=("decision", "constraint"))

    outcome = _offer(
        session, project, policy, content="Somebody prefers tabs.", category="preference"
    )

    assert outcome["outcome"] == "rejected"
    assert outcome["reason"] == mem.REJECTED_CATEGORY


def test_capture_switched_off_keeps_nothing(store):
    session, project, repo = store

    outcome = _offer(
        session, project, Policy(capture_mode="off"), content="A decision.", category="decision"
    )

    assert outcome["reason"] == mem.REJECTED_CAPTURE_OFF


def test_explicit_mode_keeps_only_what_somebody_asked_for(store):
    session, project, repo = store
    policy = Policy(capture_mode="explicit")

    unasked = _offer(session, project, policy, content="A decision.", category="decision")
    asked = _offer(
        session, project, policy, content="Another decision.", category="decision", explicit=True
    )

    assert unasked["reason"] == mem.REJECTED_EXPLICIT_ONLY
    assert asked["outcome"] == "proposed"


def test_being_asked_for_does_not_get_a_credential_through(store):
    """`explicit` raises what modes accept a candidate. It never bypasses
    a safety gate, which is the whole reason the gates are ordered."""
    session, project, repo = store

    outcome = _offer(
        session,
        project,
        Policy(capture_mode="explicit"),
        content="remember that the key is AKIAIOSFODNN7EXAMPLE",
        category="fact",
        explicit=True,
    )

    assert outcome["reason"].startswith("secret:")


def test_a_transcript_sized_body_is_refused(store, suggesting):
    session, project, repo = store

    outcome = _offer(session, project, suggesting, content="x " * 1200, category="fact")

    assert outcome["reason"] == mem.REJECTED_TOO_LONG


def test_personal_capture_is_off_unless_the_policy_allows_it(store, suggesting):
    """A memory filed against the wrong scope is found by the wrong
    sessions, and nobody goes looking for it in the right one."""
    session, project, repo = store

    refused = mem.consider(
        session,
        [mem.Candidate(content="Prefers small diffs.", category="preference")],
        policy=suggesting,
        project=project,
        scope="personal",
    )[0]
    allowed = mem.consider(
        session,
        [mem.Candidate(content="Prefers small diffs.", category="preference")],
        policy=replace(suggesting, allow_personal=True),
        project=project,
        scope="personal",
    )[0]

    assert refused["reason"] == mem.REJECTED_NO_SCOPE
    assert allowed["outcome"] == "proposed"


def test_one_bad_candidate_does_not_cost_the_others(store, suggesting):
    """Called with a list. A single unusable item must not take the rest
    with it, or an agent learns to offer one at a time."""
    session, project, repo = store

    outcomes = mem.consider(
        session,
        [
            mem.Candidate(content="A real decision.", category="decision"),
            mem.Candidate(content="key AKIAIOSFODNN7EXAMPLE", category="fact"),
            mem.Candidate(content="A real constraint.", category="constraint"),
        ],
        policy=suggesting,
        project=project,
    )

    assert [o["outcome"] for o in outcomes] == ["proposed", "rejected", "proposed"]
    assert [o["index"] for o in outcomes] == [0, 1, 2]


# --- duplicates ---------------------------------------------------------------


def test_the_same_thing_twice_does_not_repeat(store, suggesting):
    """Acceptance criterion 9."""
    session, project, repo = store
    first = _offer(
        session, project, suggesting, content="The API allows 20 a second.", category="fact"
    )

    second = _offer(
        session, project, suggesting, content="The API allows 20 a second.", category="fact"
    )

    assert second["outcome"] == "duplicate"
    assert second["duplicate_of"] == first["id"]
    assert len(list_memories(session, status=None)) == 1


def test_the_same_thing_in_other_words_does_not_repeat(store, suggesting):
    """An exact hash catches a copy and paste. Somebody rephrasing is the
    normal case and needs the similarity check."""
    session, project, repo = store
    mem.remember(
        session,
        content="Use SQLite advisory locking rather than Redis for the job queue.",
        category="decision",
        project=project,
    )

    outcome = _offer(
        session,
        project,
        suggesting,
        content="Use advisory locking in SQLite for the job queue rather than Redis.",
        category="decision",
    )

    assert outcome["outcome"] == "duplicate"


def test_something_genuinely_different_is_not_called_a_duplicate(store, suggesting):
    session, project, repo = store
    mem.remember(
        session, content="Use advisory locks for the queue.", category="decision", project=project
    )

    outcome = _offer(
        session,
        project,
        suggesting,
        content="Deploys happen on Tuesdays, never on a Friday.",
        category="fact",
    )

    assert outcome["outcome"] == "proposed"


# --- contradictions -----------------------------------------------------------


def test_a_contradiction_is_flagged_rather_than_kept_quietly(store, suggesting):
    """Acceptance criterion 10. Two memories saying different numbers, both
    active, is worse than either one alone."""
    session, project, repo = store
    mem.remember(
        session,
        content="The API is rate limited to 20 requests a second.",
        category="fact",
        project=project,
        confidence="confirmed",
    )

    outcome = _offer(
        session,
        project,
        suggesting,
        content="The API is rate limited to 50 requests a second.",
        category="fact",
        confidence="inferred",
    )

    assert outcome["outcome"] == "proposed"
    assert outcome["possible_conflict_with"]["confidence"] == "confirmed"


def test_a_near_identical_disagreement_is_a_conflict_not_a_duplicate(store, suggesting):
    """Found by testing. Two sentences differing only in a number are the
    most similar a contradiction ever gets, so checking for sameness first
    filed the clearest possible conflict as a restatement and dropped it."""
    session, project, repo = store
    mem.remember(
        session,
        content="The API is rate limited to 20 requests a second.",
        category="fact",
        project=project,
    )

    outcome = _offer(
        session,
        project,
        suggesting,
        content="The API is rate limited to 50 requests a second.",
        category="fact",
    )

    assert outcome["outcome"] != "duplicate"
    assert "possible_conflict_with" in outcome


def test_an_inference_cannot_quietly_replace_something_confirmed(store, suggesting):
    """The rule both source documents name. Approving is refused until
    somebody says the new one replaces the old."""
    session, project, repo = store
    from uuid import UUID

    mem.remember(
        session,
        content="The API is rate limited to 20 requests a second.",
        category="fact",
        project=project,
        confidence="confirmed",
    )
    outcome = _offer(
        session,
        project,
        suggesting,
        content="The API is rate limited to 50 requests a second.",
        category="fact",
        confidence="inferred",
    )

    with pytest.raises(ValidationError, match="may contradict"):
        mem.decide(session, memory_id=UUID(outcome["id"]), decision="approve")


def test_saying_it_is_a_correction_supersedes_the_old_one(store, suggesting):
    session, project, repo = store
    from uuid import UUID

    old, _ = mem.remember(
        session,
        content="The API is rate limited to 20 requests a second.",
        category="fact",
        project=project,
    )
    outcome = _offer(
        session,
        project,
        suggesting,
        content="The API is rate limited to 50 requests a second.",
        category="fact",
    )

    mem.decide(
        session,
        memory_id=UUID(outcome["id"]),
        decision="approve",
        supersede_conflict=True,
    )

    assert get_memory(session, old.id).status == "superseded"
    found = mem.recall(session, query="rate limited", project_id=project.id)["memories"]
    assert len(found) == 1
    assert "50" in found[0]["summary"]


# --- automatic capture --------------------------------------------------------


def test_auto_safe_keeps_a_confirmed_ordinary_memory(store, automatic):
    session, project, repo = store

    outcome = _offer(
        session,
        project,
        automatic,
        content="Must stay usable offline for seven days.",
        category="constraint",
        confidence="confirmed",
    )

    assert outcome["outcome"] == "committed"
    assert mem.recall(session, query="offline seven days", project_id=project.id)["memories"]


def test_auto_safe_still_asks_about_a_guess(store, automatic):
    session, project, repo = store

    outcome = _offer(
        session,
        project,
        automatic,
        content="Maybe we should move to Kafka one day.",
        category="decision",
        confidence="speculative",
    )

    assert outcome["outcome"] == "proposed"


def test_auto_safe_still_asks_about_a_category_needing_approval(store, automatic):
    session, project, repo = store

    outcome = _offer(
        session,
        project,
        automatic,
        content="Prefers small diffs in review.",
        category="preference",
        confidence="confirmed",
    )

    assert outcome["outcome"] == "proposed"


def test_auto_safe_never_commits_something_that_may_contradict(store, automatic):
    """Automatic is for the uncontroversial. A conflict is by definition
    the case where somebody has to look."""
    session, project, repo = store
    mem.remember(
        session,
        content="The API is rate limited to 20 requests a second.",
        category="fact",
        project=project,
    )

    outcome = _offer(
        session,
        project,
        automatic,
        content="The API is rate limited to 50 requests a second.",
        category="fact",
        confidence="confirmed",
    )

    assert outcome["outcome"] == "proposed"


def test_auto_safe_stops_at_its_daily_quota(store):
    """A runaway agent should cost a bounded number of memories, not a
    catalog nobody can read."""
    session, project, repo = store
    policy = Policy(capture_mode="auto_safe", max_auto_commits_per_day=2)

    outcomes = mem.consider(
        session,
        [mem.Candidate(content=text, category="fact") for text in UNRELATED],
        policy=policy,
        project=project,
    )

    assert [o["outcome"] for o in outcomes] == [
        "committed",
        "committed",
        "proposed",
        "proposed",
    ]


def test_suggestions_stop_at_their_quota(store):
    session, project, repo = store
    policy = Policy(max_suggestions_per_session=2)

    outcomes = mem.consider(
        session,
        [mem.Candidate(content=text, category="fact") for text in UNRELATED],
        policy=policy,
        project=project,
    )

    assert [o["outcome"] for o in outcomes[:2]] == ["proposed", "proposed"]
    assert all(o["reason"] == mem.REJECTED_QUOTA for o in outcomes[2:])


# --- similarity ----------------------------------------------------------------


def test_similarity_is_between_nothing_and_everything():
    assert mem.similarity("", "anything") == 0.0
    assert mem.similarity("the same words here", "the same words here") == 1.0
    assert 0.0 < mem.similarity("use advisory locks", "use advisory locking") < 1.0


# --- who may keep what -----------------------------------------------------------


def test_an_agent_cannot_keep_a_category_a_person_must_approve(store, suggesting):
    session, project, repo = store
    allowing = replace(
        suggesting, allow_categories=(*suggesting.allow_categories, "preference")
    )
    outcome = _offer(
        session,
        project,
        allowing,
        content="Prefers tabs over spaces in every file.",
        category="preference",
    )
    assert outcome["outcome"] == "proposed", outcome
    memory_id = __import__("uuid").UUID(outcome["id"])

    with pytest.raises(ValidationError, match="a person approves preference"):
        mem.decide(session, memory_id=memory_id, decision="approve", surface="agent")

    kept = mem.decide(session, memory_id=memory_id, decision="approve", surface="cli")
    assert kept["outcome"] == "approved"


def test_an_agent_may_relay_an_ordinary_decision_and_the_record_says_so(store, suggesting):
    import json

    from flanner.database import MemoryEventModel

    session, project, repo = store
    outcome = _offer(
        session, project, suggesting, content="Use advisory locks.", category="decision"
    )
    memory_id = __import__("uuid").UUID(outcome["id"])

    mem.decide(session, memory_id=memory_id, decision="approve", surface="agent")

    approved = (
        session.query(MemoryEventModel).filter_by(memory_id=memory_id, action="approved").one()
    )
    assert json.loads(approved.detail)["surface"] == "agent"


def test_editing_a_suggestion_keeps_who_proposed_it(store, suggesting):
    """The editor used to replace the proposer, losing where it came from."""
    session, project, repo = store
    outcome = mem.consider(
        session,
        [mem.Candidate(content="Use advisory locks.", category="decision")],
        policy=suggesting,
        project=project,
        created_by="claude",
    )[0]

    edited = mem.decide(
        session,
        memory_id=__import__("uuid").UUID(outcome["id"]),
        decision="edit",
        content="Use advisory locks, not Redis.",
        created_by="raj",
        surface="cli",
    )

    assert get_memory(session, __import__("uuid").UUID(edited["id"])).created_by == "claude"
