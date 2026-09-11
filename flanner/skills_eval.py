"""Comparing a candidate skill against a baseline, honestly.

This module records comparisons; it does not run them. Nothing here
calls a provider, and that is a decision rather than an omission: the
PRD keeps external model processing off until a provider, a content
boundary and a budget have been chosen, and a module that quietly
reached the network would make that setting a lie.

So a suite is defined here, its fixtures are hashed here, and results
are entered here by whatever actually ran them — a harness, a script, a
person. What this module is for is making the resulting claim readable:

  - every cell names its fixture, its skill version, its model **and**
    its harness, because a model is not an agent and an endpoint result
    says nothing about behaviour inside Claude Code;
  - a cell nobody ran is reported as not run, never as a zero, because an
    empty cell and a bad score are different facts;
  - a difference against the baseline is reported with its sample size
    beside it, and a one-trial difference is called out as one trial.

A comparison here is evidence about a defined task in a stated
environment. It is not a claim about production, and the report says so
in as many words.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from .database import (
    SkillEvalCaseModel,
    SkillModelProfileModel,
    SkillTrialModel,
)

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
RESULTS = (PASSED, FAILED, ERROR, SKIPPED)

#: Printed on every comparison. Shortening it would be the whole failure
#: mode of this feature.
LIMITS = (
    "Flanner stored these results. It ran none of them, and nothing here calls a model.",
    "A result here is about these fixtures in this environment. It is not "
    "a claim about production.",
    "A model is not an agent: an endpoint result does not show how a skill "
    "behaves inside a harness. Both are named on every cell.",
    "Cells with no trial are not run, not zero.",
)


def fixture_hash(prompt: str, rubric: str) -> str:
    """Over the task and how it is judged, together.

    Both, because moving the goalposts is as good a way to produce a
    flattering number as changing the question.
    """
    digest = hashlib.sha256(f"{prompt}\0{rubric}".encode()).hexdigest()
    return f"sha256:{digest}"


# --- fixtures and profiles ----------------------------------------------------


def add_case(session: Session, suite: str, name: str, prompt: str, rubric: str) -> dict[str, Any]:
    """Write down a task, and how a result on it will be judged."""
    row = SkillEvalCaseModel(
        suite=suite,
        name=name,
        prompt=prompt,
        rubric=rubric,
        fixture_hash=fixture_hash(prompt, rubric),
    )
    session.add(row)
    session.commit()
    # A dict, not the row: `get_session` returns a new session per call,
    # so an instance handed back outlives the session that loaded it.
    return {"id": str(row.id), "suite": suite, "name": name, "fixture_hash": row.fixture_hash}


def add_profile(
    session: Session,
    name: str,
    *,
    provider: str = "",
    model: str = "",
    revision: str | None = None,
    harness: str = "",
    harness_version: str = "",
    settings_hash: str = "",
) -> dict[str, Any]:
    """Register a model-and-harness combination results can be filed under."""
    existing = session.query(SkillModelProfileModel).filter_by(name=name).one_or_none()
    if existing is not None:
        return _profile_view(existing)
    row = SkillModelProfileModel(
        name=name,
        provider=provider,
        model=model,
        revision=revision,
        harness=harness,
        harness_version=harness_version,
        settings_hash=settings_hash,
    )
    session.add(row)
    session.commit()
    return _profile_view(row)


def _profile_view(row: SkillModelProfileModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "provider": row.provider,
        "model": row.model,
        "revision": row.revision,
        "harness": row.harness,
        "harness_version": row.harness_version,
    }


def cases(session: Session, suite: str) -> list[SkillEvalCaseModel]:
    return list(
        session.query(SkillEvalCaseModel)
        .filter_by(suite=suite)
        .order_by(SkillEvalCaseModel.name)
        .all()
    )


def profiles(session: Session) -> list[SkillModelProfileModel]:
    return list(session.query(SkillModelProfileModel).order_by(SkillModelProfileModel.name).all())


# --- results ------------------------------------------------------------------


def record_trial(
    session: Session,
    suite: str,
    case_name: str,
    profile_name: str,
    *,
    skill_hash: str = "",
    baseline: bool = False,
    result: str = SKIPPED,
    score: str = "",
    measured_by: str = "",
    note: str = "",
) -> dict[str, Any]:
    """File one result, with who measured it.

    `measured_by` is required for anything but a skip. A number with no
    stated source cannot be checked, and an unfalsifiable number in a
    comparison table is worse than a missing one.
    """
    if result not in RESULTS:
        raise ValueError(f"{result} is not one of {', '.join(RESULTS)}")
    if result != SKIPPED and not measured_by:
        raise ValueError("say who or what measured this; an unattributed result is not evidence")

    case = session.query(SkillEvalCaseModel).filter_by(suite=suite, name=case_name).one_or_none()
    if case is None:
        raise ValueError(f"no case {case_name!r} in suite {suite!r}")
    profile = session.query(SkillModelProfileModel).filter_by(name=profile_name).one_or_none()
    if profile is None:
        raise ValueError(f"no model profile named {profile_name!r}")

    row = SkillTrialModel(
        suite=suite,
        case_id=case.id,
        profile_id=profile.id,
        skill_hash=skill_hash,
        baseline=baseline,
        result=result,
        score=score,
        measured_by=measured_by,
        note=note,
    )
    session.add(row)
    session.commit()
    return {"id": str(row.id), "suite": suite, "case": case_name, "result": result}


# --- the matrix ---------------------------------------------------------------


def matrix(session: Session, suite: str) -> dict[str, Any]:
    """Every fixture against every profile and skill version.

    Built by walking the full grid rather than the rows that exist, so a
    combination nobody ran appears as a cell saying so. Listing only what
    was run is how a comparison ends up looking complete when most of it
    was never attempted.
    """
    suite_cases = cases(session, suite)
    if not suite_cases:
        return {
            "suite": suite,
            "cells": [],
            "arms": [],
            "profiles": [],
            "summary": {},
            "limits": list(LIMITS),
            "notes": [f"No fixtures defined for {suite!r}."],
        }

    trials = list(session.query(SkillTrialModel).filter_by(suite=suite).all())
    by_id = {p.id: p for p in profiles(session)}

    # The arms being compared: the no-skill baseline plus each skill
    # version anybody filed a result for.
    arms = sorted({t.skill_hash for t in trials} | {""})
    used_profiles = sorted({by_id[t.profile_id].name for t in trials if t.profile_id in by_id})

    cells = []
    for case in suite_cases:
        for profile_name in used_profiles or ["(no profile)"]:
            for arm in arms:
                matching = [
                    t
                    for t in trials
                    if t.case_id == case.id
                    and t.skill_hash == arm
                    and by_id.get(t.profile_id)
                    and by_id[t.profile_id].name == profile_name
                ]
                cells.append(_cell(case, profile_name, arm, matching))

    return {
        "suite": suite,
        "arms": [a or "(no skill: baseline)" for a in arms],
        "profiles": [
            {
                "name": p.name,
                "provider": p.provider,
                "model": p.model,
                "revision": p.revision,
                "harness": p.harness,
                "harness_version": p.harness_version,
            }
            for p in profiles(session)
            if p.name in used_profiles
        ],
        "cells": cells,
        "summary": _summary(cells, arms),
        "limits": list(LIMITS),
        "notes": _coverage_notes(cells),
    }


def _cell(
    case: SkillEvalCaseModel,
    profile_name: str,
    arm: str,
    matching: list[SkillTrialModel],
) -> dict[str, Any]:
    passed = sum(1 for t in matching if t.result == PASSED)
    return {
        "case": case.name,
        "fixture_hash": case.fixture_hash,
        "profile": profile_name,
        "arm": arm or "(no skill: baseline)",
        "skill_hash": arm,
        "trials": len(matching),
        "passed": passed,
        "result": "not run" if not matching else _verdict(matching),
        "measured_by": sorted({t.measured_by for t in matching if t.measured_by}),
        "notes": [t.note for t in matching if t.note],
    }


def _verdict(matching: list[SkillTrialModel]) -> str:
    outcomes = {t.result for t in matching}
    if outcomes == {PASSED}:
        return PASSED
    if PASSED not in outcomes:
        return sorted(outcomes)[0]
    return "mixed"


def _summary(cells: list[dict[str, Any]], arms: list[str]) -> dict[str, Any]:
    """Pass rates per arm, each with the sample size that produced it."""
    out: dict[str, Any] = {}
    for arm in arms:
        label = arm or "(no skill: baseline)"
        mine = [c for c in cells if c["skill_hash"] == arm]
        trials = sum(c["trials"] for c in mine)
        passed = sum(c["passed"] for c in mine)
        out[label] = {
            "cells": len(mine),
            "not_run": sum(1 for c in mine if c["trials"] == 0),
            "trials": trials,
            "passed": passed,
            # Deliberately not a percentage when there is nothing to divide.
            "pass_rate": round(passed / trials, 3) if trials else None,
            "reads_as": _reads_as(trials),
        }
    return out


def _reads_as(trials: int) -> str:
    if trials == 0:
        return "nothing was run"
    if trials < 5:
        return f"{trials} trial(s); too few to read as a rate"
    return f"{trials} trials"


def _coverage_notes(cells: list[dict[str, Any]]) -> list[str]:
    missing = sum(1 for c in cells if c["trials"] == 0)
    notes = []
    if missing:
        notes.append(
            f"{missing} of {len(cells)} cells were never run. They are shown as "
            "not run and are not counted anywhere as a failure."
        )
    unattributed = sum(1 for c in cells if c["trials"] and not c["measured_by"])
    if unattributed:
        notes.append(f"{unattributed} cell(s) carry results with no stated source.")
    return notes


def regressions(session: Session, suite: str) -> list[dict[str, Any]]:
    """Where an arm did worse than the baseline on the same fixture.

    Reported per fixture and per profile, never averaged into one number.
    A skill that helps on one model and hurts on another is the finding;
    an average hides exactly that.
    """
    grid = matrix(session, suite)
    baseline = {(c["case"], c["profile"]): c for c in grid["cells"] if c["skill_hash"] == ""}

    found = []
    for cell in grid["cells"]:
        if not cell["skill_hash"] or cell["trials"] == 0:
            continue
        against = baseline.get((cell["case"], cell["profile"]))
        if against is None or against["trials"] == 0:
            continue
        if cell["passed"] < against["passed"]:
            found.append(
                {
                    "case": cell["case"],
                    "profile": cell["profile"],
                    "arm": cell["arm"],
                    "passed": cell["passed"],
                    "baseline_passed": against["passed"],
                    "trials": cell["trials"],
                    "baseline_trials": against["trials"],
                    "reads_as": _reads_as(min(cell["trials"], against["trials"])),
                    "fixture_hash": cell["fixture_hash"],
                }
            )
    return found
