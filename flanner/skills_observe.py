"""Watching which skills an agent actually uses, when told to.

Off unless somebody turns it on, per agent and per project. What is kept
is that a named skill was invoked, when, and by which local session — no
prompt, no reply, no file contents. There is no fallback that reads
conversation archives; if the agent does not report a use through a
supported interface, the use is simply not known about.

Two limits are load-bearing, and both are about not lying:

  - Only explicit invocations are recorded. Claude Code puts every skill's
    name and description in front of the model whether or not it is used,
    and nothing on this machine can see which of those the model read. A
    number counting loads would be made up, so there is not one.
  - A use that cannot be tied to one package with confidence is stored
    with no version rather than assigned to the newest. A wrong
    attribution quietly corrupts every comparison drawn from it.

Coverage windows are why the reports can be trusted at all. Zero uses
means one of two very different things, and only a record of when this
machine was watching separates "nobody used it" from "nothing was
listening".
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from . import skills_ops
from .database import (
    ProjectModel,
    SkillCoverageWindowModel,
    SkillModel,
    SkillObservationModel,
    SkillPolicyModel,
    SkillVersionModel,
    get_project_by_root,
)

#: What a skill use can be. Only the first is ever recorded today; the
#: others exist so a report can say a column is empty because nothing can
#: see it, rather than because nothing happened.
INVOCATION = "invocation"
LOAD = "load"

#: How far a row can be trusted. `observed` is the harness telling us
#: directly. Nothing else is produced yet, and inventing one would make
#: the label meaningless.
OBSERVED = "observed"
INFERRED = "inferred"
UNKNOWN = "unknown"

#: Groups a report uses when the harness did not say which model ran.
UNKNOWN_GROUP = "unknown"

DEFAULT_RETENTION_DAYS = 30


def utcnow() -> datetime:
    """Naive UTC, matching the DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- policy -------------------------------------------------------------------


def _project_or_raise(session: Session, project_root: Path) -> ProjectModel:
    found = get_project_by_root(session, str(project_root))
    if found is None:
        raise ValueError(f"{project_root} is not a flanner project; run `flanner init` first")
    return found


def policy_for(session: Session, agent: str, project: ProjectModel) -> SkillPolicyModel | None:
    return (
        session.query(SkillPolicyModel).filter_by(agent=agent, project_id=project.id).one_or_none()
    )


def observing(session: Session, agent: str, project: ProjectModel) -> bool:
    held = policy_for(session, agent, project)
    return bool(held and held.observing)


def enable(
    session: Session,
    project_root: Path,
    agent: str = "claude-code",
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    """Start watching this agent in this project, and say so on the record.

    Opening a coverage window is half the point of the operation: without
    one, everything recorded afterwards is a set of events with no stated
    period, and a count with no period behind it cannot be read.
    """
    project = _project_or_raise(session, project_root)
    held = policy_for(session, agent, project)
    if held is None:
        held = SkillPolicyModel(agent=agent, project_id=project.id)
        session.add(held)

    already = held.observing
    held.observing = True
    held.retention_days = retention_days
    held.updated_at = utcnow()

    window = _open_window(session, agent, project)
    if window is None:
        window = SkillCoverageWindowModel(
            agent=agent,
            project_id=project.id,
            capabilities=_capabilities(agent),
        )
        session.add(window)

    session.commit()
    return {
        "agent": agent,
        "project": project.name,
        "already_on": already,
        "retention_days": retention_days,
        "window_started_at": window.started_at.isoformat() + "Z",
    }


def disable(
    session: Session,
    project_root: Path,
    agent: str = "claude-code",
    reason: str = "turned off",
) -> dict[str, Any]:
    """Stop watching, and close the window rather than deleting it.

    Nothing already recorded is removed here. Turning collection off and
    destroying what was collected are different decisions, and running
    them together would make one of them impossible to take back.
    """
    project = _project_or_raise(session, project_root)
    held = policy_for(session, agent, project)
    if held is not None:
        held.observing = False
        held.updated_at = utcnow()

    window = _open_window(session, agent, project)
    if window is not None:
        window.ended_at = utcnow()
        window.gap_reason = reason

    session.commit()
    return {
        "agent": agent,
        "project": project.name,
        "was_on": bool(held and window is not None),
        "kept_observations": _count_for(session, agent, project),
    }


def _open_window(
    session: Session, agent: str, project: ProjectModel
) -> SkillCoverageWindowModel | None:
    return (
        session.query(SkillCoverageWindowModel)
        .filter_by(agent=agent, project_id=project.id, ended_at=None)
        .order_by(SkillCoverageWindowModel.started_at.desc())
        .first()
    )


def _capabilities(agent: str) -> str:
    from . import skills_adapters

    adapter = skills_adapters.adapter_for(agent)
    if adapter is None:
        return ""
    able = adapter.capability()
    return ",".join(
        name
        for name, on in (
            ("discover", able.discover),
            ("resolve_precedence", able.resolve_precedence),
            ("observe", able.observe),
            ("install", able.install),
        )
        if on
    )


def _count_for(session: Session, agent: str, project: ProjectModel) -> int:
    return (
        session.query(SkillObservationModel).filter_by(agent=agent, project_id=project.id).count()
    )


# --- recording ----------------------------------------------------------------


def dedupe_key(session_ref: str, skill: str, at: str) -> str:
    """One key per event, so a replayed hook writes one row.

    The harness's own event fields go into it rather than a timestamp we
    invent here: a retried hook is the same event and must collide, and
    two genuine uses a second apart must not.
    """
    digest = hashlib.sha256(f"{session_ref}\0{skill}\0{at}".encode()).hexdigest()
    return f"sha256:{digest}"


def attribute(
    session: Session, skill_name: str, agent: str, project_root: Path | None
) -> SkillVersionModel | None:
    """Which package version this use was of, or None when it is not clear.

    Deliberately strict. Several copies of a name can be installed at once
    and only one is loaded, so the answer is the effective copy's latest
    recorded version — and if the catalog has never seen that copy, or the
    scan finds no effective copy at all, the answer is nothing.
    """
    effective = [
        p for p in skills_ops.scan(project_root, agent) if p.name == skill_name and p.effective
    ]
    if len(effective) != 1:
        return None

    skill = (
        session.query(SkillModel)
        .filter_by(name=skill_name, agent=agent, directory=effective[0].directory)
        .one_or_none()
    )
    if skill is None:
        return None
    return (
        session.query(SkillVersionModel)
        .filter_by(skill_id=skill.id, manifest_hash=effective[0].manifest_hash)
        .one_or_none()
    )


def record_use(session: Session, payload: dict[str, Any], agent: str = "claude-code") -> str:
    """Store one skill invocation reported by the harness.

    Returns a word saying what happened, which is what the hook prints for
    a person reading its log: `recorded`, `duplicate`, `not-observing`,
    `no-project`, or `not-a-skill`.

    Anything it cannot make sense of is dropped and counted against the
    open coverage window, never guessed at. A dropped event that is on the
    record is a gap somebody can find; a guessed one is a wrong number
    nobody can.
    """
    if (payload.get("tool_name") or "") != "Skill":
        return "not-a-skill"

    tool_input = payload.get("tool_input") or {}
    skill_name = str(tool_input.get("skill") or "").strip()
    cwd = payload.get("cwd") or ""
    if not skill_name or not cwd:
        return _dropped(session, agent, cwd, "event was missing the skill name or directory")

    from .git_integration import find_git_root

    root = find_git_root(str(cwd))
    project = get_project_by_root(session, root) if root else None
    if project is None:
        return "no-project"
    if not observing(session, agent, project):
        return "not-observing"

    session_ref = str(payload.get("session_id") or "")
    key = dedupe_key(session_ref, skill_name, str(payload.get("timestamp") or utcnow()))
    if session.query(SkillObservationModel).filter_by(dedupe_key=key).one_or_none() is not None:
        return "duplicate"

    version = attribute(session, skill_name, agent, Path(root) if root else None)
    session.add(
        SkillObservationModel(
            dedupe_key=key,
            agent=agent,
            project_id=project.id,
            session_ref=session_ref,
            skill_name=skill_name,
            skill_version_id=version.id if version else None,
            kind=INVOCATION,
            evidence="hook",
            certainty=OBSERVED,
            agent_version=str(payload.get("model") or payload.get("agent_version") or ""),
        )
    )
    session.commit()
    return "recorded"


def _dropped(session: Session, agent: str, cwd: str, reason: str) -> str:
    """Count an event that arrived and could not be stored."""
    from .git_integration import find_git_root

    root = find_git_root(str(cwd)) if cwd else None
    project = get_project_by_root(session, root) if root else None
    if project is not None:
        window = _open_window(session, agent, project)
        if window is not None:
            window.dropped_count += 1
            window.gap_reason = reason
            session.commit()
    return "dropped"


def run_hook(raw_stdin: str, session: Session) -> str:
    """Read a PostToolUse payload and record it. Never raises.

    A hook that throws is a hook that interrupts somebody's work, and no
    usage statistic is worth that.
    """
    try:
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
        return record_use(session, payload)
    except Exception:
        return "error"


# --- reporting ----------------------------------------------------------------


def status(session: Session) -> dict[str, Any]:
    """Where observation is on, and what it has managed to see."""
    rows = []
    for held in session.query(SkillPolicyModel).all():
        project = session.query(ProjectModel).filter_by(id=held.project_id).one_or_none()
        if project is None:
            continue
        windows = (
            session.query(SkillCoverageWindowModel)
            .filter_by(agent=held.agent, project_id=project.id)
            .order_by(SkillCoverageWindowModel.started_at.desc())
            .all()
        )
        rows.append(
            {
                "agent": held.agent,
                "project": project.name,
                "project_root": project.project_root,
                "observing": held.observing,
                "retention_days": held.retention_days,
                "observations": _count_for(session, held.agent, project),
                "dropped": sum(w.dropped_count for w in windows),
                "windows": [
                    {
                        "started_at": w.started_at.isoformat() + "Z",
                        "ended_at": w.ended_at.isoformat() + "Z" if w.ended_at else None,
                        "capabilities": w.capabilities,
                        "dropped_count": w.dropped_count,
                        "gap_reason": w.gap_reason,
                    }
                    for w in windows
                ],
            }
        )
    return {
        "scopes": sorted(rows, key=lambda r: (r["project"], r["agent"])),
        "notes": [
            "Only explicit invocations are recorded. Claude Code shows every "
            "skill's description to the model without reporting it, so loads "
            "are unknown rather than zero.",
        ],
    }


def usage(
    session: Session, project_root: Path | None = None, days: int = DEFAULT_RETENTION_DAYS
) -> dict[str, Any]:
    """Which skills were used, over a stated window, with its coverage.

    Every count here is bounded by that window and by whether anything was
    watching during it, and both travel with the numbers. A count without
    them invites the reading that a skill is unused when the truth may be
    that nothing was ever listening.
    """
    since = utcnow() - timedelta(days=days)
    query = session.query(SkillObservationModel).filter(SkillObservationModel.occurred_at >= since)
    project = None
    if project_root is not None:
        project = get_project_by_root(session, str(project_root))
        if project is None:
            return _empty_usage(days, since, "this directory is not a flanner project")
        query = query.filter(SkillObservationModel.project_id == project.id)

    events = query.all()
    by_skill: dict[str, dict[str, Any]] = {}
    for event in events:
        row = by_skill.setdefault(
            event.skill_name,
            {
                "skill": event.skill_name,
                "invocations": 0,
                "attributed": 0,
                "last_used_at": None,
                "by_model": {},
                # One bucket per day of the window, oldest first. A total
                # and a last-used date cannot tell seventeen uses spread
                # over a fortnight from seventeen in one afternoon.
                "by_day": [0] * days,
                "certainty": event.certainty,
            },
        )
        row["invocations"] += 1
        row["attributed"] += 1 if event.skill_version_id else 0
        stamp = event.occurred_at.isoformat() + "Z"
        if row["last_used_at"] is None or stamp > row["last_used_at"]:
            row["last_used_at"] = stamp
        group = event.agent_version or UNKNOWN_GROUP
        row["by_model"][group] = row["by_model"].get(group, 0) + 1
        row["by_day"][_day_index(event.occurred_at, since, days)] += 1

    for row in by_skill.values():
        # Largest first, so the model that did most of the work reads first
        # rather than whichever happened to be seen first.
        row["by_model"] = dict(sorted(row["by_model"].items(), key=lambda kv: (-kv[1], kv[0])))

    windows = session.query(SkillCoverageWindowModel)
    if project is not None:
        windows = windows.filter(SkillCoverageWindowModel.project_id == project.id)
    watched = [w for w in windows.all() if w.ended_at is None or w.ended_at >= since]

    installed = {p.name for p in skills_ops.scan(project_root) if p.effective}
    used = set(by_skill)
    return {
        "window_days": days,
        "since": since.isoformat() + "Z",
        "coverage": {
            "watching": bool(watched),
            "windows": len(watched),
            "dropped": sum(w.dropped_count for w in watched),
            "observes": [w.capabilities for w in watched],
            # One flag per day, oldest first: was anything watching. The
            # sentence above says how many periods; this says which days,
            # which is the difference between trusting the claim and
            # seeing it.
            "days": _covered_days(watched, since, days),
        },
        "rows": sorted(by_skill.values(), key=lambda r: (-r["invocations"], r["skill"])),
        # Not "unused". Without coverage this is every installed skill, and
        # the wording has to survive that case being the common one.
        "not_observed": sorted(installed - used),
        "notes": [
            "Counts explicit invocations only; a skill read by the model "
            "without being invoked does not appear.",
            "Rows with fewer attributed than invocations had uses that could "
            "not be tied to one package version.",
        ]
        + ([] if watched else ["Nothing was watching in this window, so zero means nothing."]),
    }


def _day_index(when: datetime, since: datetime, days: int) -> int:
    """Which bucket a moment falls in, clamped to the window.

    Both sides are naive UTC, which is what `utcnow` returns and what the
    columns hold. A first draft coerced a timezone onto one of them and
    produced exactly the mismatch it was written to prevent.
    """
    return max(0, min(days - 1, (when - since).days))


def _covered_days(watched: list[Any], since: datetime, days: int) -> list[bool]:
    """Whether anything was watching on each day of the window."""
    strip = [False] * days
    for window in watched:
        first = _day_index(max(window.started_at, since), since, days)
        last = days - 1 if window.ended_at is None else _day_index(window.ended_at, since, days)
        for index in range(first, last + 1):
            strip[index] = True
    return strip


def _empty_usage(days: int, since: datetime, why: str) -> dict[str, Any]:
    return {
        "window_days": days,
        "since": since.isoformat() + "Z",
        "coverage": {
            "watching": False,
            "windows": 0,
            "dropped": 0,
            "observes": [],
            "days": [False] * days,
        },
        "rows": [],
        "not_observed": [],
        "notes": [why],
    }


#: A window watched for less of itself than this is a shortlist rather
#: than a verdict, and the page has to say so before it says anything else.
THIN_COVERAGE = 0.8


def attention(
    session: Session,
    project_root: Path | None = None,
    days: int = DEFAULT_RETENTION_DAYS,
    report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """What the usage data says somebody should do, worst first.

    The table answers "what did I use". This answers "what do I do about
    it", which is the question somebody actually arrived with, and every
    item on it is a join the data always supported and nobody made.

    The first is a caveat rather than a task. Deciding a skill is dead
    from a window that was only half watched is exactly the wrong answer,
    so the thinness of the evidence is said before any of it is used.

    `report` lets a caller that already ran `usage` hand it back; the scan
    inside it hashes every file in every package.
    """
    if report is None:
        report = usage(session, project_root, days)
    strip = report["coverage"].get("days") or []
    covered = sum(1 for day in strip if day)
    rows = {row["skill"]: row for row in report["rows"]}
    found: list[dict[str, Any]] = []

    if strip and covered < len(strip) * THIN_COVERAGE:
        found.append(
            {
                "code": "thin_coverage",
                "severity": skills_ops.ADVICE,
                "headline": f"Watched on {covered} of {len(strip)} days",
                "detail": (
                    "Everything below is a shortlist rather than a verdict. A skill "
                    "used once a month can look unused in a window nobody watched."
                ),
                "skills": [],
            }
        )

    # Being used while a copy of it sits shadowed is the failure this whole
    # product exists to catch, and until now the two halves sat in
    # different tables.
    packages = skills_ops.scan(project_root)
    copies: dict[str, list[Any]] = {}
    for package in packages:
        copies.setdefault(package.name, []).append(package)

    shadowed = sorted(
        name for name in rows if any(not copy.effective for copy in copies.get(name, []))
    )
    if shadowed:
        found.append(
            {
                "code": "used_and_shadowed",
                "severity": skills_ops.DEFECT,
                "headline": f"{len(shadowed)} skill(s) in use have a shadowed copy",
                "detail": (
                    "Your agent invoked these, and each has another copy on disk it "
                    "never reads. Editing the wrong one is the usual cause of "
                    "'my change did nothing'."
                ),
                "skills": shadowed,
            }
        )

    unattributed = sorted(
        name for name, row in rows.items() if row["attributed"] < row["invocations"]
    )
    if unattributed:
        found.append(
            {
                "code": "used_but_unattributed",
                "severity": skills_ops.DEFECT,
                "headline": f"{len(unattributed)} name(s) in use match no package",
                "detail": (
                    "Invoked, but nothing on disk carries that name now. It was "
                    "renamed, removed, or something is still asking for it."
                ),
                "skills": unattributed,
            }
        )

    quiet = report["not_observed"]
    if quiet and report["coverage"]["watching"]:
        found.append(
            {
                "code": "not_observed",
                "severity": skills_ops.ADVICE,
                "headline": f"{len(quiet)} loaded skill(s) were not used",
                "detail": (
                    "Each one costs context on every session that loads it. Worth a "
                    "look, not a deletion: this window is what was seen, not what exists."
                ),
                "skills": quiet,
            }
        )
    return found


def to_csv(report: dict[str, Any]) -> str:
    """The usage rows as CSV, for a spreadsheet rather than a program.

    The coverage note rides along as a comment line. A bare table of counts
    detached from the window it covers is exactly the artefact that ends up
    in a slide saying something untrue.
    """
    out = io.StringIO()
    out.write(
        f"# window: last {report['window_days']} days since {report['since']}; "
        f"watching={report['coverage']['watching']}; "
        f"dropped={report['coverage']['dropped']}\n"
    )
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["skill", "invocations", "attributed", "last_used_at", "models"])
    for row in report["rows"]:
        writer.writerow(
            [
                row["skill"],
                row["invocations"],
                row["attributed"],
                row["last_used_at"] or "",
                ";".join(f"{k}={v}" for k, v in sorted(row["by_model"].items())),
            ]
        )
    return out.getvalue()


# --- retention ----------------------------------------------------------------


def purge(
    session: Session, project_root: Path | None = None, older_than_days: int | None = None
) -> dict[str, int]:
    """Delete recorded uses. Everything, or everything past its retention.

    Deleting is only ever asked for, never scheduled. An automatic purge
    would eventually destroy the one week somebody needed, on a day nobody
    was thinking about it.
    """
    query = session.query(SkillObservationModel)
    if project_root is not None:
        project = get_project_by_root(session, str(project_root))
        if project is None:
            return {"deleted": 0}
        query = query.filter(SkillObservationModel.project_id == project.id)
    if older_than_days is not None:
        query = query.filter(
            SkillObservationModel.occurred_at < utcnow() - timedelta(days=older_than_days)
        )

    deleted = query.delete(synchronize_session=False)
    session.commit()
    return {"deleted": int(deleted)}


def expire(session: Session) -> dict[str, int]:
    """Drop what has outlived the retention each scope asked for."""
    deleted = 0
    for held in session.query(SkillPolicyModel).all():
        cutoff = utcnow() - timedelta(days=held.retention_days)
        deleted += int(
            session.query(SkillObservationModel)
            .filter(
                SkillObservationModel.project_id == held.project_id,
                SkillObservationModel.agent == held.agent,
                SkillObservationModel.occurred_at < cutoff,
            )
            .delete(synchronize_session=False)
        )
    session.commit()
    return {"deleted": deleted}
