"""What each page of the web UI's Curb section shows (Curb PRD §11.3).

Read models only. Each function takes what Curb already worked out and
returns plain rows for a template, in the page's words: Open and Closed for
uncontrolled and controlled, Sent and Readable for classes A and B.

A credential's name or a secret's location goes into a row only when
`shown` is true. The route sets that from the browser's own reveal, and
nothing here decides it.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (
    agent_paths,
    curb_app,
    curb_attribution,
    curb_ci,
    curb_fix,
    curb_inventory,
    curb_log,
    curb_observe,
    curb_policy,
    curb_reach,
    curb_report,
    curb_store,
    curb_sweep,
    curb_tester,
)
from .curb_context import AGENTS, BASELINE, LABELS
from .curb_reach import AgentReport
from .curb_severity import ENFORCED

#: The page's word for each way in or out.
WAYS = {
    curb_reach.FILE_TOOLS: "Its own file tools",
    curb_reach.SHELL_FILES: "Shell commands, on files",
    curb_reach.SHELL_NETWORK: "Shell commands, on the network",
    curb_reach.WEB: "Web fetch and search",
    curb_reach.MCP: "MCP servers",
    curb_reach.APPS: "Apps and hosted tools",
    curb_reach.MODEL: "The model provider",
}
READS = (curb_reach.FILE_TOOLS, curb_reach.SHELL_FILES)
OPEN = (curb_reach.UNCONTROLLED, curb_reach.UNKNOWN)
#: A pill's colour for each level of risk.
TONES = {"High": "stale", "Medium": "suspect", "Low": "fresh"}
OWNERS = {"user": "You", "project": "A project", "admin": "Your administrator"}
EXPOSURES = {
    curb_sweep.SENT: ("sent", "Sent", "stale"),
    curb_sweep.READABLE: ("readable", "Readable", "aging"),
    curb_sweep.BLOCKED: ("blocked", "Blocked", "drift-empty"),
}
#: How many records and commits a page reads at most.
MAX_RECORDS = 2000
COMMITS_EACH = 15
MAX_COMMITS = 200
REVISION = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./~^@{}-]*(\.\.\.?[A-Za-z0-9_][A-Za-z0-9_./~^@{}-]*)?"
)


# --- words ------------------------------------------------------------------------------

_PLURAL = re.compile(r"\b(\d+) ([^.;:()]*?)\(s\)")


def count(number: int, noun: str, plural: str | None = None) -> str:
    """`1 secret`, `2 secrets`."""
    return f"{number:,} {noun if number == 1 else plural or noun + 's'}"


def said(text: str) -> str:
    """A phrase starting a cell or a line: its first letter upper-cased, the rest as it is.

    Not Jinja's `capitalize`, which lower-cases the rest and turns the
    month in "on 2 October" into "october".
    """
    return text[:1].upper() + text[1:]


def sentence(text: str) -> str:
    """Curb's terminal phrasing as a sentence: capital, full stop, real plurals."""
    said = _PLURAL.sub(lambda m: f"{m[1]} {m[2]}{'' if m[1] == '1' else 's'}", str(text))
    said = said.replace("`", "").replace(" (shown in the window)", "").strip()
    if not said:
        return ""
    return said[0].upper() + said[1:] + ("" if said[-1] in ".!?" else ".")


def when(stamp: float | None, *, now: float | None = None) -> str:
    """How long ago, or the day once that is clearer."""
    if not stamp:
        return "never"
    seconds = (time.time() if now is None else now) - stamp
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size and seconds < 7 * 86400:
            return count(int(seconds // size), unit) + " ago"
    return "just now" if seconds < 60 else "on " + day(stamp)


def day(stamp: float) -> str:
    moment = time.localtime(stamp)
    return f"{moment.tm_mday} {time.strftime('%B', moment)}"


def clock(stamp: float) -> str:
    moment = time.localtime(stamp)
    return f"{moment.tm_mday} {time.strftime('%b, %H:%M:%S', moment)}"


def _epoch(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _listed(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def search(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Rows whose `find` text holds every word. Hidden names are not in it to be found."""
    words = query.lower().split()
    return [r for r in rows if all(w in r["find"] for w in words)] if words else rows


# --- this machine, worked out once ----------------------------------------------------


@dataclass
class Machine:
    """Each agent's launch outside any project: what every project starts from."""

    reports: list[AgentReport]
    skipped: list[str]
    inventories: list[curb_inventory.AgentInventory]
    plan: curb_fix.Plan

    @property
    def defaults(self) -> list[AgentReport]:
        return [r for r in self.reports if r.context.source == "default"]


def machine() -> Machine:
    """Assess each agent as it starts outside any project, and plan its fixes."""
    home = Path.home()
    _, contexts, skipped = curb_report.contexts(curb_store.outside(), None, None, ())
    reports = curb_report.assess(contexts)
    defaults = [r for r in reports if r.context.source == "default"]
    jobs = curb_inventory.scheduled_jobs(home)
    inventories = [
        curb_inventory.gather(r.context.agent, r.settings, project=None, jobs=jobs)
        for r in defaults
    ]
    plan = curb_fix.plan(defaults, home=home, platform=sys.platform, env=os.environ)
    return Machine(reports, skipped, inventories, plan)


def project_reports(folder: Path) -> list[AgentReport]:
    """Each agent's default launch from one project folder."""
    _, contexts, _ = curb_report.contexts(folder, None, None, ())
    return curb_report.assess([c for c in contexts if c.source == "default"])


# --- reach: one agent's card ---------------------------------------------------------


def _way(channel: curb_reach.Channel) -> dict[str, Any]:
    if channel.state in OPEN:
        kind, said = "open", "Open" if channel.state == curb_reach.UNCONTROLLED else "Cannot tell"
    elif channel.state == curb_reach.CONTROLLED:
        proved = channel.evidence == ENFORCED
        kind, said = ("proved", "Closed, proved") if proved else ("closed", "Closed")
    else:
        kind, said = "quiet", "Not used" if channel.state == curb_reach.ABSENT else "Always on"
    link = None
    if channel.state in OPEN:
        link = {
            curb_reach.AUTO: ("Fix ready", "fixes"),
            curb_reach.GUIDED: ("Only you can close this", "fixes"),
        }.get(channel.disposition)
    return {
        "name": WAYS[channel.key],
        "kind": kind,
        "state": said,
        "why": sentence(channel.why),
        "close": sentence(channel.fix) if channel.fix and channel.state in OPEN else "",
        "link": link,
    }


def _gate(channels: Sequence[curb_reach.Channel], *, open_: bool) -> str:
    """One gate of the picture: open, closed, or closed and proved by a test."""
    if open_:
        return "open"
    closed = [c for c in channels if c.state == curb_reach.CONTROLLED]
    return "proved" if closed and all(c.evidence == ENFORCED for c in closed) else "closed"


def card(report: AgentReport) -> dict[str, Any]:
    """One agent launch: its risk, the picture, and each way in and out."""
    reads = [c for c in report.channels if c.key in READS]
    sends = [c for c in report.channels if c.key not in READS]
    egress = [c for c in sends if c.key in curb_reach.EGRESS]
    readable = len(report.readable)
    out = any(c.state in OPEN for c in egress)
    if not readable:
        lede = "It cannot read your credentials."
    elif out:
        lede = f"It can read {count(readable, 'credential')} and send data out."
    else:
        lede = f"It can read {count(readable, 'credential')}, but every way out is closed."
    proved = sum(1 for c in report.channels if c.evidence == ENFORCED)
    if proved:
        lede += f" A test has proved {count(proved, 'block')}."
    level = report.verdict.severity
    return {
        "agent": report.context.agent,
        "label": report.context.label,
        "level": level,
        "tone": TONES.get(level, "quiet"),
        "guess": not report.supported,
        "version": report.version,
        "baseline": BASELINE[report.context.agent],
        "lede": lede,
        "why": sentence(report.verdict.text),
        "credentials": readable,
        "read_gate": _gate(reads, open_=bool(readable)),
        "send_gate": _gate(egress, open_=out),
        "reads": [_way(c) for c in reads],
        "sends": [_way(c) for c in sends],
        "assumed": [sentence(line) for line in report.assumed],
        "not_checked": [sentence(line) for line in report.not_checked],
        "kinds": dict(sorted(Counter(r.credential.category for r in report.readable).items())),
    }


def headline(reports: Sequence[AgentReport]) -> str:
    """The one sentence the Overview opens with."""
    if not reports:
        return "No supported agent was found on this machine."
    facts = []
    for report in reports:
        out = any(c.key in curb_reach.EGRESS and c.state in OPEN for c in report.channels)
        facts.append((report.context.label, bool(report.readable), out))
    names = [name for name, _, _ in facts]
    who = "Both agents" if len(facts) == 2 else _listed(names)
    if not any(reads for _, reads, _ in facts):
        return f"{'Neither agent' if len(facts) == 2 else names[0]} can read your credentials."
    if all(reads and out for _, reads, out in facts):
        return f"{who} can read your credentials and send data out."
    if all(reads and not out for _, reads, out in facts):
        return f"{who} can read your credentials, but cannot send data out."
    said = []
    for name, reads, out in facts:
        if not reads:
            said.append(f"{name} cannot read your credentials.")
        elif out:
            said.append(f"{name} can read your credentials and send data out.")
        else:
            said.append(f"{name} can read your credentials, but cannot send data out.")
    return " ".join(said)


# --- agents ----------------------------------------------------------------------------


def agents(
    held: Machine,
    *,
    shown: bool,
    logging: Mapping[str, bool],
    signing: Iterable[str],
    guides: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """Installed agents, what each loads at start, and what Curb could not read."""
    signs = set(signing)
    reports = {r.context.agent: r for r in held.defaults}
    installed, loaded, problems = [], [], []
    for item in held.inventories:
        owners = list(dict.fromkeys(OWNERS[x.controller] for x in item.layers if x.present))
        if item.version is None:
            version = "Not found on your PATH"
        elif item.supported:
            version = f"Version {item.version}, which Curb has been tested with"
        else:
            version = f"Version {item.version}. Curb was tested with {BASELINE[item.agent]}"
            problems.append(
                {
                    "tone": "warn",
                    "title": f"Curb has not been tested with {item.label} {item.version}.",
                    "body": f"It was tested with {BASELINE[item.agent]}, so everything shown "
                    f"for {item.label} is a best guess. A newer flanner may know this version.",
                    "command": "uv tool upgrade flanner",
                }
            )
        for layer in item.layers:
            if layer.error:
                problems.append(
                    {
                        "tone": "bad",
                        "title": f"Curb cannot read {item.label}'s {layer.name} settings.",
                        "body": sentence(f"the file {layer.error}")
                        + " Fix the file, then check again. Until then, what is shown for "
                        f"{item.label} leaves that file out.",
                        "command": "",
                    }
                )
        installed.append(
            {
                "label": item.label,
                "version": version,
                "owners": _listed(owners) if owners else "No settings found",
                "logging": bool(logging.get(item.agent)),
                "signs": item.agent in signs,
            }
        )
        for server in item.mcp:
            note = ""
            if server.env_names:
                note = f"It is given {count(len(server.env_names), 'environment variable')}."
                if shown:
                    note += " " + ", ".join(server.env_names)
            loaded.append(_loaded(server.name, "mcp", "MCP server", item, server.controller, note))
        for hook in item.hooks:
            name = f"{hook.event} hook" + (f" ×{hook.count}" if hook.count > 1 else "")
            loaded.append(_loaded(name, "hook", "Hook", item, hook.controller, ""))
        for job in item.jobs:
            launch = next((r for r in held.reports if r.context is job.context), None)
            level = launch.verdict.severity if launch else ""
            note = sentence(job.problem or "Starts the agent with nobody watching")
            row = _loaded(job.name, "job", "Scheduled job", item, "user", note)
            loaded.append({**row, "level": level, "tone": TONES.get(level, "")})
    for project, agent in guides:
        label = LABELS.get(agent, agent)
        loaded.append(
            {
                "name": "Curb's guide for agents",
                "kind": "skill",
                "kind_label": "Skill",
                "agent": label,
                "by": f"Project {project}",
                "note": "",
                "level": "",
                "tone": "",
                "find": f"curb's guide for agents skill {label} {project}".lower(),
            }
        )
    order = {"job": 0, "mcp": 1, "hook": 2, "skill": 3}
    loaded.sort(key=lambda r: (order[r["kind"]], r["agent"], r["name"].lower()))
    missing = [a for a in AGENTS if a not in reports]
    return {
        "installed": installed,
        "loaded": loaded,
        "problems": problems,
        "missing": [LABELS[a] for a in missing],
    }


def _loaded(
    name: str, kind: str, label: str, item: curb_inventory.AgentInventory, owner: str, note: str
) -> dict[str, Any]:
    by = OWNERS.get(owner, owner)
    return {
        "name": name,
        "kind": kind,
        "kind_label": label,
        "agent": item.label,
        "by": by,
        "note": note,
        "level": "",
        "tone": "",
        "find": f"{name} {label} {item.label} {by}".lower(),
    }


# --- leaks -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Leak:
    """One finding as the web process keeps it: its kind and place, never its value."""

    type: str
    category: str
    exposure: str
    path: str
    line: int
    validation: str | None
    #: Keyed digests: `file` names the file in a form without saying where it is.
    file: str
    secret: str


@dataclass
class Scan:
    """The last scan this process ran. Memory only: a restart drops the places."""

    leaks: list[Leak]
    view: dict[str, Any]
    at: float
    validated: bool = False


def keep(report: curb_sweep.SweepReport) -> Scan:
    """A scan's findings without their secrets, for this process to hold."""
    leaks = [
        Leak(
            f.match.rule_name,
            f.artifact.category,
            f.exposure,
            str(f.artifact.path),
            f.match.line,
            f.validation,
            f.path_digest,
            f.secret_digest,
        )
        for f in report.findings
    ]
    return Scan(leaks, curb_sweep.redacted(report), time.time(), report.validated)


def _works(outcome: str | None) -> tuple[str, str]:
    word = (outcome or "").lower()
    if not word or word in ("none", "not_attempted"):
        return "quiet", "Not checked"
    if word in ("valid", "active"):
        return "open", "Still works"
    if word in ("invalid", "revoked", "inactive"):
        return "proved", "No longer works"
    return "quiet", word.replace("_", " ").capitalize()


def leaks(
    scan: Scan | None,
    stored: Mapping[str, Any] | None,
    *,
    shown: bool,
    projects: Sequence[tuple[str, Path]] = (),
) -> dict[str, Any]:
    """Secrets found, from the scan in memory or else the redacted copy on disk."""
    home = str(Path.home())
    rows = []
    if scan is not None:
        view, at, live = scan.view, scan.at, True
        for leak in scan.leaks:
            row = _leak(leak.type, leak.category, leak.exposure, leak.validation)
            place = leak.path.replace(home, "~", 1) if leak.path.startswith(home) else leak.path
            inside = next((n for n, root in projects if _under(leak.path, root)), "")
            row.update(
                where=f"{place}, line {leak.line:,}" if shown else "",
                project=(inside or "This machine") if shown else "",
                file=leak.file,
                removable=True,
            )
            if shown:
                row["find"] += f" {place} {inside}".lower()
            rows.append(row)
    elif stored is not None:
        view, at, live = dict(stored), float(stored.get("created") or 0), False
        for found in stored.get("findings") or []:
            # The stored report keeps a rule's id, never its name or the place.
            kind = f"Rule {found['rule']}" if found.get("rule") else "Secret"
            rows.append(
                _leak(
                    kind,
                    str(found.get("category")),
                    str(found.get("class")),
                    found.get("validation"),
                )
            )
    else:
        return {"scanned": False, "rows": [], "stats": [], "live": False}
    rows.sort(key=lambda r: (r["urgency"], r["type"]))
    by_class = view.get("by_class") or {}
    return {
        "scanned": True,
        "live": live,
        "at": at,
        "files": int(view.get("files_scanned") or 0),
        "secrets": int(view.get("secrets") or 0),
        "sent": int(by_class.get(curb_sweep.SENT) or 0),
        "validated": "validation" in view,
        "not_checked": [sentence(line) for line in view.get("not_checked") or []],
        "no_agent": not view.get("launches", True),
        "rows": rows,
        "stats": [
            {
                "key": EXPOSURES[key][0],
                "value": int(by_class.get(key) or 0),
                "colour": EXPOSURES[key][2],
                "text": text,
            }
            for key, text in (
                (curb_sweep.SENT, "Sent to a model provider. <b>Rotate now.</b>"),
                (curb_sweep.READABLE, "An agent can read it. Rotate or move it."),
                (curb_sweep.BLOCKED, "On disk, but blocked. Nothing to do."),
            )
        ],
    }


def _under(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _leak(kind: str, category: str, exposure: str, validation: Any) -> dict[str, Any]:
    key, label, colour = EXPOSURES.get(exposure, ("blocked", "Blocked", "drift-empty"))
    works_kind, works = _works(None if validation is None else str(validation))
    urgency = {"Still works": 0, "No longer works": 3}.get(works, 1 if key == "sent" else 2)
    return {
        "type": kind,
        "category": category,
        "exposure": key,
        "exposure_label": label,
        "colour": colour,
        "works": works,
        "works_kind": works_kind,
        "urgency": urgency if key != "blocked" else 4,
        "where": "",
        "project": "",
        "file": "",
        "removable": False,
        "find": f"{kind} {category} {label} {works}".lower(),
    }


# --- fixes -----------------------------------------------------------------------------


def _settings_of(path: str) -> str:
    """Whose settings a backed-up file is, in words that name no place."""
    for agent, folder in (
        ("claude", agent_paths.claude_config_dir()),
        ("codex", agent_paths.codex_home()),
    ):
        if _under(path, folder) or Path(path).name == ".claude.json" and agent == "claude":
            return f"{LABELS[agent]} settings"
    return "Agent settings"


def fixes(held: Machine, *, shown: bool) -> dict[str, Any]:
    """What Curb can change, what only the person can, and what it changed before."""
    ready = [
        {"text": sentence(action), "agent": LABELS[edit.agent]}
        for edit in held.plan.edits
        for action in edit.actions
    ]
    lines = []
    if shown:
        for edit in held.plan.edits:
            try:
                before = edit.path.read_text(encoding="utf-8")
            except OSError:
                before = ""
            diff = difflib.unified_diff(
                before.splitlines(), edit.text.splitlines(), lineterm="", n=2
            )
            body = [
                {"kind": "add" if x[0] == "+" else "del" if x[0] == "-" else "", "text": x}
                for x in list(diff)[2:]
                if not x.startswith("@@")
            ]
            lines.append({"file": str(edit.path), "lines": body})
    return {
        "ready": ready,
        "lines": lines,
        "yours": [sentence(line) for line in dict.fromkeys(held.plan.guided)],
        "refused": [sentence(line) for line in dict.fromkeys(held.plan.refused)],
        "applied": applied(),
    }


def applied() -> list[dict[str, Any]]:
    """Each backup Curb holds, newest first: an undo for seven days."""
    folder = curb_fix.backups_dir()
    rows = []
    for manifest in (
        sorted(folder.glob("*/manifest.json"), reverse=True) if folder.is_dir() else []
    ):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            created = float(data["created"])
            whose = list(dict.fromkeys(_settings_of(str(e["path"])) for e in data["files"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        rows.append(
            {
                "id": manifest.parent.name,
                "at": created,
                "what": _listed(whose) if whose else "Agent settings",
                "files": len(data["files"]),
                "until": created + curb_fix.BACKUP_DAYS * 86400,
            }
        )
    return rows


# --- tests -----------------------------------------------------------------------------

_TRIED = {
    curb_tester.BLOCKED: "blocked",
    curb_tester.ALLOWED: "got through",
    curb_tester.INCONCLUSIVE: "unclear",
    curb_tester.UNSUPPORTED: "not available here",
}


@dataclass
class Block:
    """One thing a test can prove, for one agent."""

    id: str
    report: AgentReport
    label: str
    target: curb_tester.Target | None = None
    probe: curb_tester.Probe | None = None

    @property
    def testable(self) -> bool:
        target = self.target
        return target is None or bool(target.folder or target.relative)

    @property
    def subject(self) -> str | None:
        """What a stored proof names. A scratch test is never stored."""
        if self.probe is not None:
            return f"probe:{self.probe.kind}"
        return str(self.target.folder) if self.target and self.target.folder else None


def blocks(reports: Sequence[AgentReport]) -> list[Block]:
    """Every block a test can try, for each agent's default launch."""
    home, env = Path.home(), dict(os.environ)
    out = []
    for report in reports:
        agent = report.context.agent
        for target in curb_tester.targets(report, home):
            name = f"{agent}|file|{target.folder}|{target.relative}|{target.reason}"
            out.append(Block(name, report, f"{target.label} kept out of reach", target=target))
        for probe in curb_tester.probes(report, env):
            out.append(Block(f"{agent}|{probe.kind}", report, probe.label, probe=probe))
    return out


def _verdict(outcomes: Mapping[str, str], scratch: bool) -> tuple[str, str, str]:
    tested = [o for o in outcomes.values() if o != curb_tester.UNSUPPORTED]
    if tested and all(o == curb_tester.BLOCKED for o in tested):
        if scratch:
            return "fresh", "Held in a copy", "It held in a scratch copy of the project."
        return "fresh", "Proved", "The agent tried and failed."
    if any(o == curb_tester.ALLOWED for o in tested):
        return "stale", "Not proved", "At least one way got through."
    return "aging", "Unclear", "The agent declined, failed, or stopped to ask. Run it again."


def tests(
    reports: Sequence[AgentReport],
    *,
    shown: bool,
    fresh: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Each block and its last result, and the fake credentials a test plants.

    `fresh` holds this process's last run by block id, which is how a scratch
    result shows at all: those are never written down.
    """
    try:
        held = json.loads((curb_store.curb_dir() / "proofs.json").read_text(encoding="utf-8"))
        proofs = [p for p in held.get("proofs", []) if isinstance(p, dict)]
    except (OSError, ValueError):
        proofs = []
    key = curb_store.digest_key() if proofs else b""
    rows = []
    for block in blocks(reports):
        tone, result, why, at, tries = (
            "quiet",
            "Not tested",
            "Run the tests to find out.",
            None,
            {},
        )
        latest = (fresh or {}).get(block.id)
        if not block.testable and block.target is not None:
            result, why = "Cannot test", sentence(block.target.reason)
        elif latest is not None:
            tries, at = dict(latest["outcomes"]), latest["at"]
            tone, result, why = _verdict(tries, bool(latest.get("scratch")))
        elif proofs and block.subject is not None:
            context = curb_store.digest(curb_tester.context_key(block.report.context), key)
            target = curb_store.digest(block.subject, key)
            found = [
                p for p in proofs if p.get("context") == context and p.get("target") == target
            ]
            if found:
                proof = max(found, key=lambda p: float(p.get("time") or 0))
                tries, at = dict(proof.get("outcomes") or {}), float(proof.get("time") or 0)
                if proof.get("settings") != curb_tester.settings_digest(block.report, key):
                    tone, result = "aging", "Out of date"
                    why = "The settings changed after this test. Run it again."
                else:
                    tone, result, why = _verdict(tries, bool(proof.get("scratch")))
        agent = block.report.context.label
        rows.append(
            {
                "id": block.id,
                "name": block.label,
                "agent": agent,
                "agent_key": block.report.context.agent,
                "tone": tone,
                "result": result,
                "why": why,
                "at": at,
                "testable": block.testable,
                "tries": [f"With {how}: {_TRIED.get(o, o)}" for how, o in tries.items()],
                "find": f"{block.label} {agent} {result}".lower(),
            }
        )
    runnable = [r for r in rows if r["testable"]]
    per_agent = Counter(str(r["agent_key"]) for r in runnable)
    decoys = curb_tester.inventory()
    return {
        "rows": rows,
        "runnable": len(runnable),
        "proved": sum(1 for r in rows if r["result"] == "Proved"),
        "tested": any(r["at"] for r in rows),
        "cost": [sentence(curb_tester.estimate(n, agent)) for agent, n in per_agent.items()],
        "decoys": len(decoys),
        "decoys_until": min((d.expires for d in decoys), default=None),
        "decoy_places": [d.path for d in decoys] if shown else [],
    }


# --- activity --------------------------------------------------------------------------

_KINDS = {
    curb_reach.FILE_TOOLS: ("files", "A file"),
    curb_reach.SHELL_FILES: ("shell", "A command on files"),
    curb_reach.SHELL_NETWORK: ("shell", "A command on the network"),
    curb_reach.WEB: ("web", "A web page or search"),
    curb_reach.MCP: ("mcp", "An MCP tool"),
}
_DECIDED = {"requested": "Asked", "ran": "Ran", "denied": "Denied", "failed": "Failed"}


def activity(reports: Sequence[AgentReport]) -> dict[str, Any]:
    """Logging for each agent, what each was seen using, and the newest records."""
    held = curb_log.records()
    by_agent = {r.context.agent: r for r in reports}
    logging, used = [], []
    for agent in AGENTS:
        if agent not in by_agent and not curb_observe.hooks_on(agent):
            continue
        mine = [r for r in held if r.get("kind") == "tool" and r.get("agent") == agent]
        on = curb_observe.hooks_on(agent)
        logging.append(
            {
                "agent": agent,
                "label": LABELS[agent],
                "on": on,
                "records": len(mine),
                "since": min((float(r.get("time") or 0) for r in mine), default=None),
            }
        )
        if not mine:
            continue
        seen = curb_observe.observe(agent, by_agent.get(agent))
        used.append(
            {
                "label": LABELS[agent],
                "days": round(seen.days),
                "sessions": seen.sessions,
                "whole": seen.state == curb_observe.OBSERVED,
                "gaps": [sentence(gap) for gap in seen.gaps],
                "ideas": [sentence(idea) for idea in seen.ideas],
                "rows": [
                    {
                        "what": WAYS[channel],
                        "used": count(times, "time") if times else "Not seen",
                        "blind": not seen.covered[channel],
                    }
                    for channel, times in seen.seen.items()
                ],
            }
        )
    rows = []
    for record in reversed(held[-MAX_RECORDS:]):
        stamp = float(record.get("time") or 0)
        if record.get("kind") == "approval":
            decision = "Approved" if record.get("decision") == "granted" else "Refused"
            row = {
                "kind": "approval",
                "tool": "Approval",
                "agent": "You",
                "what": sentence(str(record.get("summary") or "")),
                "decision": decision,
                "bad": decision == "Refused",
            }
        elif record.get("kind") == "tool":
            kind, what = _KINDS.get(str(record.get("channel")), ("other", "Something else"))
            program = record.get("program")
            decision = _DECIDED.get(str(record.get("decision")), str(record.get("decision")))
            row = {
                "kind": kind,
                "tool": str(record.get("tool")),
                "agent": LABELS.get(str(record.get("agent")), str(record.get("agent"))),
                "what": f"{what}: {program}" if program and kind == "shell" else what,
                "decision": decision,
                "bad": decision in ("Denied", "Failed"),
            }
        else:
            continue
        row.update(at=stamp, when=clock(stamp))
        row["find"] = f"{row['tool']} {row['agent']} {row['what']} {row['decision']}".lower()
        rows.append(row)
    return {
        "logging": logging,
        "used": used,
        "rows": rows,
        "total": len(held),
        "any_on": any(item["on"] for item in logging),
    }


# --- projects: reach -------------------------------------------------------------------


def differs(here: Sequence[AgentReport], base: Sequence[AgentReport]) -> str:
    """What a project changes, against the same agents started in the home folder."""
    said = []
    start = {r.context.agent: r for r in base}
    for report in here:
        other = start.get(report.context.agent)
        if other is None:
            continue
        more = len(report.readable) - len(other.readable)
        states = {c.key: c.state for c in other.channels}
        for channel in report.channels:
            before = states.get(channel.key)
            if before is None or (before in OPEN) == (channel.state in OPEN):
                continue
            now = "open" if channel.state in OPEN else "closed"
            said.append(f"{WAYS[channel.key]} is {now} here for {report.context.label}")
        if more:
            change = f"{abs(more):,} {'more' if more > 0 else 'fewer'}"
            noun = "credential" if abs(more) == 1 else "credentials"
            said.append(f"{report.context.label} reads {change} {noun}")
    unique = list(dict.fromkeys(said))
    return sentence("; ".join(unique)) if unique else "Nothing. It is the same as this machine."


def credentials(reports: Sequence[AgentReport], *, shown: bool) -> list[dict[str, Any]]:
    """Each credential found, and which agents can read it. Names only when shown."""
    rows: dict[str, dict[str, Any]] = {}
    home = str(Path.home())
    for report in reports:
        for reach in report.reach:
            found = reach.credential
            key = f"{found.kind}|{found.label}|{found.paths}|{found.names}"
            row = rows.get(key)
            if row is None:
                places = [str(p).replace(home, "~", 1) for p in found.paths]
                name = ", ".join(x for x in (found.identity, *found.names) if x)
                row = rows[key] = {
                    "kind": found.label.replace("`", ""),
                    "category": found.category[:1].upper() + found.category[1:],
                    "name": (name or "No name") if shown else "",
                    "where": (
                        ", ".join(places)
                        or ("Reached by running a command" if found.via_shell else "")
                    )
                    if shown
                    else "",
                    "readers": [],
                    "blocked": [],
                    "find": f"{found.label} {found.category}".lower(),
                }
                if shown:
                    row["find"] += f" {name} {' '.join(places)}".lower()
            (row["readers"] if reach.via else row["blocked"]).append(report.context.label)
    return sorted(rows.values(), key=lambda r: (not r["readers"], r["category"], r["kind"]))


# --- projects: CI, apps, commits ------------------------------------------------------


def ci(checks: Sequence[tuple[str, Path]]) -> dict[str, Any]:
    """Agent steps in each project's workflows, worst first, and the safe fixes on offer."""
    rows, problems, fixable = [], [], []
    for name, root in checks:
        steps, failed = curb_ci.check(root)
        problems += [sentence(f"{name}: {line}") for line in failed]
        for workflow in sorted({s.workflow for s in steps}):
            done = curb_ci.fix(root / workflow, write=False)
            if done:
                fixable.append({"project": name, "workflow": workflow, "fixes": done})
        for step in steps:
            level = step.rule.severity
            rows.append(
                {
                    "level": level,
                    "tone": TONES.get(level, "quiet"),
                    "where": f"{step.workflow}:{step.line}",
                    "step": f"{step.agent}, step “{step.name}”",
                    "project": name,
                    "why": sentence(curb_ci.message(step).split("; ", 1)[-1]),
                    "fix": sentence("; ".join(step.fixes)) if step.fixes else "",
                    "find": f"{level} {step.workflow} {step.agent} {step.name} {name}".lower(),
                }
            )
    rank = {"High": 0, "Medium": 1, "Low": 2}
    rows.sort(key=lambda r: (rank.get(r["level"], 3), r["project"], r["where"]))
    return {
        "rows": rows,
        "problems": problems,
        "fixable": fixable,
        "fixes": sum(len(item["fixes"]) for item in fixable),
        "risky": sum(1 for r in rows if r["level"] != "Low"),
    }


_SHAPES = {curb_app.SINGLE: "One call", curb_app.TOOLS: "Uses tools", curb_app.LOOP: "Loop"}


def apps(audits: Sequence[tuple[str, Path]]) -> dict[str, Any]:
    """Each LLM call in each project's Python code, the flagged ones first."""
    rows, problems = [], []
    for name, root in audits:
        calls, failed = curb_app.audit(root)
        problems += [sentence(f"{name}: {line}") for line in failed]
        for call in calls:
            if call.unchecked_output:
                flag, tone = (
                    f"The model's answer reaches {_listed(call.unchecked_output)}",
                    "stale",
                )
            elif call.untrusted_input:
                flag, tone = "Text from outside is in the same function", "aging"
            else:
                flag, tone = "", ""
            rows.append(
                {
                    "where": f"{call.path}:{call.line}",
                    "project": name,
                    "library": call.library,
                    "shape": _SHAPES.get(call.shape, call.shape),
                    "flag": flag,
                    "tone": tone,
                    "find": f"{call.path} {name} {call.library} {call.shape} {flag}".lower(),
                }
            )
    rows.sort(key=lambda r: ({"stale": 0, "aging": 1}.get(r["tone"], 2), r["project"], r["where"]))
    return {"rows": rows, "problems": problems, "flagged": sum(1 for r in rows if r["flag"])}


_SIGNED = {
    curb_attribution.ATTRIBUTED: ("signed", "fresh", "Agent key"),
    curb_attribution.RETIRED: ("signed", "fresh", "Agent key, since replaced"),
    curb_attribution.REVOKED: ("untrusted", "stale", "Not trusted"),
    curb_attribution.UNKNOWN: ("unknown", "aging", "Cannot tell"),
    curb_attribution.UNATTRIBUTED: ("none", "quiet", "Not signed by an agent"),
}


def signing_keys(*, joined: bool) -> list[dict[str, Any]]:
    """Each agent's signing key: public facts only."""
    held = curb_attribution.keys()["keys"]
    rows = []
    for agent in AGENTS:
        entry = held.get(agent)
        folder = agent_paths.claude_config_dir() if agent == "claude" else agent_paths.codex_home()
        if entry is None and not folder.exists():
            continue
        created = float((entry or {}).get("created") or 0)
        rows.append(
            {
                "agent": agent,
                "label": LABELS[agent],
                "fingerprint": (entry or {}).get("fingerprint", ""),
                "known": (
                    "Your organization"
                    if (entry or {}).get("registered")
                    else "This machine only"
                    + (". Your organization hears at the next check-in" if joined else "")
                ),
                "replace_by": created + curb_attribution.ROTATE_DAYS * 86400 if entry else None,
                "due": bool(entry)
                and time.time() > created + curb_attribution.ROTATE_DAYS * 86400,
            }
        )
    return rows


def commits(
    repos: Sequence[tuple[str, Path]],
    revision: str,
    *,
    listing: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Who signed each commit. `revision` is empty for each project's newest commits."""
    if revision and not REVISION.fullmatch(revision):
        return {
            "rows": [],
            "problems": [],
            "error": "Enter a commit or a range, such as main..HEAD.",
        }
    known = curb_attribution.revoked()
    now = datetime.now(timezone.utc)
    rows, problems = [], []
    for name, root in repos:
        try:
            if revision:
                shas = curb_attribution.commits(revision, root)[:MAX_COMMITS]
            else:
                shas = curb_attribution.recent(root, COMMITS_EACH)
            for sha in shas:
                raw = curb_attribution.raw_commit(sha, root)
                verdict = curb_attribution.state_of(raw, listing, known, now)
                payload, _ = curb_attribution.split_signature(raw)
                body = payload.split(b"\n\n", 1)[-1].decode("utf-8", "replace")
                subject = body.strip().splitlines()[0] if body.strip() else "(no message)"
                state, tone, label = _SIGNED.get(verdict.state, ("none", "quiet", verdict.state))
                if verdict.agent:
                    who = f"{LABELS.get(verdict.agent, verdict.agent)} on device "
                    who += (verdict.device_id or "")[:12]
                elif state == "untrusted":
                    who = "Its key was revoked"
                elif state == "unknown":
                    who = "Signed, but this machine has no fresh key list to check against"
                else:
                    who = "A person, or an agent without a key"
                rows.append(
                    {
                        "sha": sha[:7],
                        "subject": subject,
                        "project": name,
                        "state": state,
                        "tone": tone,
                        "label": label,
                        "who": who,
                        "find": f"{sha[:12]} {subject} {name} {label}".lower(),
                    }
                )
        except ValueError as failure:
            problems.append(sentence(f"{name}: git could not read that ({failure})"))
    return {"rows": rows, "problems": problems, "error": ""}


# --- team ------------------------------------------------------------------------------


def policy(state: curb_policy.State, flagged: Sequence[str], *, shown: bool) -> dict[str, Any]:
    """The org policy on this device: what it asks, whether it is met, what waits."""
    received = state.received
    if received is None:
        return {"received": False, "delegated": state.delegated_at is not None}
    asks: list[tuple[str, str]] = []
    try:
        rules = received.rules()
    except ValueError as failure:
        rules = None
        asks.append(("Rules", sentence(f"this flanner cannot read them: {failure}")))
    if rules is not None:
        places = ", ".join(rules.deny_read) if shown else count(len(rules.deny_read), "location")
        asks += [
            ("No agent may read", places if rules.deny_read else "Nothing named"),
            ("Sandbox", "Required" if rules.needs_sandbox else "Not required"),
            (
                "Commands may reach",
                "Any site"
                if rules.allowed_domains is None
                else ", ".join(rules.allowed_domains) or "No site",
            ),
            ("Web fetch and search", "Off" if rules.web_off else "Allowed"),
            (
                "MCP servers",
                "Any" if rules.mcp is None else ", ".join(s.name for s in rules.mcp) or "None",
            ),
        ]
        if rules.unknown:
            asks.append(("Rules this flanner does not know", count(len(rules.unknown), "rule")))
    applied = state.applied or {}
    pending = state.pending or {}
    unmet = {agent: rules for agent, rules in state.unmet.items() if rules}
    checked = [
        {"label": LABELS.get(agent, agent), "ok": False, "text": sentence(rule)}
        for agent, missing in unmet.items()
        for rule in missing
    ]
    verified = "unverified" not in flagged
    done = bool(applied.get("version") == received.version and not pending)
    return {
        "received": True,
        "version": received.version,
        "asks": asks,
        "flags": [
            sentence(curb_policy.FLAGS[code]) for code in flagged if code in curb_policy.FLAGS
        ],
        "pending": (
            {
                "version": pending.get("version"),
                "summary": sentence(str(pending.get("summary") or "")),
                "reasons": [sentence(str(r)) for r in pending.get("reasons") or []],
                "at": pending.get("at"),
            }
            if pending
            else None
        ),
        "rejected": (
            {
                "version": state.rejected.get("version"),
                "reason": sentence(str(state.rejected.get("reason") or "")),
                "at": state.rejected.get("at"),
            }
            if state.rejected
            else None
        ),
        "applied": applied.get("version"),
        "applied_by": {
            "person": "you approved it",
            "delegation": "applied on its own",
            "already in place": "it was already in place",
        }.get(str(applied.get("by")), ""),
        "applied_at": applied.get("at"),
        "checked_at": state.checked_at,
        "unmet": checked,
        "met": bool(state.checked_at) and not checked,
        "delegated": state.delegated_at is not None,
        "steps": [
            ("Received", "done"),
            ("Signature checked", "done" if verified else "now"),
            ("Waiting for you", "now") if pending else ("Applied", "done" if done else ""),
            ("Meets the policy", "done" if done and state.checked_at and not checked else ""),
        ],
    }


def devices(snapshot: Mapping[str, Any], *, mine: str | None) -> dict[str, Any]:
    """Each device's newest report, from the last `flanner curb fleet` on this machine."""
    rows = []
    for device in snapshot.get("rows") or []:
        held = device.get("policy") or {}
        severity = device.get("severity") or {}
        if held.get("drift"):
            tone, said = "suspect", f"No longer meets version {held.get('applied') or '?'}"
        elif held.get("pending"):
            tone, said = "aging", f"Waiting on version {held['pending']}"
        elif held.get("applied"):
            tone, said = "fresh", f"Meets version {held['applied']}"
        else:
            tone, said = "quiet", "No policy yet"
        worst = next((n for n in ("High", "Medium", "Low") if severity.get(n.lower())), "")
        exposure = device.get("exposure") or {}
        loaded = ", ".join(
            f"{LABELS.get(a.get('agent'), a.get('agent'))} {a.get('version')}"
            for a in device.get("agents") or []
        )
        verified = bool(device.get("verified"))
        needs = not verified or bool(device.get("stale")) or tone in ("suspect", "aging")
        name = str(device.get("device") or "")
        rows.append(
            {
                "name": name,
                "mine": device.get("device_id") == mine,
                "agents": loaded,
                "tone": tone,
                "policy": said,
                "worst": worst,
                "worst_tone": TONES.get(worst, "quiet"),
                "leaked": exposure.get(curb_sweep.SENT) if device.get("exposure") else None,
                "last": _epoch(device.get("last_report")),
                "quiet": bool(device.get("stale")),
                "verified": verified,
                "problems": [sentence(p) for p in device.get("problems") or []],
                "needs": needs,
                "order": (0 if tone == "suspect" else 1 if not verified else 2 if needs else 3),
                "find": f"{name} {loaded} {said} {worst}".lower(),
            }
        )
    rows.sort(key=lambda r: (r["order"], r["name"].lower()))
    return {
        "rows": rows,
        "fetched_at": snapshot.get("fetched_at"),
        "drift": sum(1 for r in rows if r["tone"] == "suspect"),
        "quiet": sum(1 for r in rows if r["quiet"]),
    }


_ALERT_PART = {
    "mcp_server_added": ("machine/agents", "See agents"),
    "deny_rule_removed": ("machine/fixes", "See fixes"),
    "sandbox_off": ("machine/fixes", "See fixes"),
    "secret_class_a": ("machine/leaks", "See leaks"),
}


def alerts(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each alert of the last 30 days, and the part of the page to act on it in."""
    rows = []
    for alert in history:
        level = str(alert.get("severity") or "").capitalize()
        part, label = _ALERT_PART.get(str(alert.get("type")), ("team/policy", "See policy"))
        who = LABELS.get(str(alert.get("agent")), "This machine")
        what = sentence(str(alert.get("said") or ""))
        stamp = float(alert.get("created_at") or 0)
        rows.append(
            {
                "at": stamp,
                "what": what,
                "who": who,
                "level": level,
                "tone": TONES.get(level, "quiet"),
                "told": "You. Your organization is next"
                if alert.get("queued")
                else "You, and your organization",
                "part": part,
                "go": label,
                "find": f"{what} {who} {level}".lower(),
            }
        )
    return rows


# --- the overview's list ---------------------------------------------------------------


@dataclass
class Next:
    """One thing to do next, most urgent first."""

    title: str
    area: str
    why: str
    go: str
    href: str
    rank: int = field(default=0, repr=False)


def do_next(
    held: Machine,
    *,
    sweep: Mapping[str, Any] | None,
    tested: Mapping[str, Any],
    logging_off: Sequence[str],
    risky_ci: Sequence[tuple[str, int]],
    pending_policy: Any,
    keys_due: Sequence[str],
) -> list[Next]:
    out = []
    by_class = (sweep or {}).get("by_class") or {}
    sent, readable = int(by_class.get("A") or 0), int(by_class.get("B") or 0)
    if sweep is None:
        out.append(
            Next(
                "Scan for leaked secrets",
                "This machine",
                "Curb looks in agent transcripts, your shell history and project .env files. "
                "Nothing leaves this machine.",
                "Open leaks",
                "/curb/machine/leaks",
            )
        )
    elif sent:
        out.append(
            Next(
                f"Rotate {count(sent, 'leaked secret')}",
                "This machine",
                "They were sent to a model provider, so treat them as public.",
                "See leaks",
                "/curb/machine/leaks",
            )
        )
    elif readable:
        out.append(
            Next(
                f"Move or rotate {count(readable, 'secret')} an agent can read",
                "This machine",
                "No agent has sent them yet, but nothing stops one.",
                "See leaks",
                "/curb/machine/leaks",
            )
        )
    ready = sum(len(edit.actions) for edit in held.plan.edits)
    if ready:
        out.append(
            Next(
                f"Apply {count(ready, 'fix', 'fixes')}",
                "This machine",
                "One approval closes what Curb can close for you. Every fix can be undone.",
                "Review fixes",
                "/curb/machine/fixes",
            )
        )
    if tested["runnable"] and tested["proved"] < tested["runnable"]:
        out.append(
            Next(
                "Prove the blocks hold",
                "This machine",
                f"Only {tested['proved']} of {count(tested['runnable'], 'block')} "
                f"{'has' if tested['proved'] == 1 else 'have'} been proved by a test."
                if tested["proved"]
                else f"None of the {count(tested['runnable'], 'block')} has been tested yet."
                if tested["runnable"] > 1
                else "The 1 block has not been tested yet.",
                "Open tests",
                "/curb/machine/tests",
            )
        )
    if logging_off:
        out.append(
            Next(
                "Turn on the activity log",
                "This machine",
                f"It shows what {_listed(list(logging_off))} really "
                f"{'uses' if len(logging_off) == 1 else 'use'}, so you can close the rest.",
                "Open activity",
                "/curb/machine/activity",
            )
        )
    for project, steps in risky_ci:
        out.append(
            Next(
                f"Fix {count(steps, 'risky CI step')}",
                f"Projects · {project}",
                "An agent runs in a workflow there with more reach than it needs.",
                "See CI",
                "/curb/projects/ci",
            )
        )
    if pending_policy:
        out.append(
            Next(
                f"Approve policy version {pending_policy}",
                "Team",
                "One of your organization's new rules waits for your yes.",
                "Review",
                "/curb/team/policy",
            )
        )
    for agent in keys_due:
        out.append(
            Next(
                f"Replace the {LABELS.get(agent, agent)} signing key",
                "Projects",
                "It is more than 90 days old.",
                "See commits",
                "/curb/projects/commits",
            )
        )
    return out
