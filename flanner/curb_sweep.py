"""The leak sweep: secrets left in agent artifacts, by exposure class (Curb PRD §10.3).

It looks where agents leave secrets behind: Claude Code transcripts, Codex
sessions and prompt history, CLAUDE.md and AGENTS.md, skills, MCP configs
and agent settings, shell history, project `.env` files, and flanner's own
plans and memories. Each secret found gets one exposure class:

- A, sent to a model provider: it is in a transcript, so it has already left
  the machine. Rotate it now.
- B, readable by an agent: some agent launch here can read the file through
  a channel no control covers, by the same check `curb map` makes.
- C, on disk but blocked: no agent launch here can read it.

Read-only. A value is held only long enough to digest it under the
per-device key (§7.2); reports carry digests, categories and classes.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agent_paths, curb_reach, curb_store, identity, skills_adapters
from .curb_context import LaunchContext
from .curb_credentials import Credential, dotenv_files
from .curb_kingfisher import Match
from .curb_settings import ClaudeSettings, CodexSettings

SENT, READABLE, BLOCKED = "A", "B", "C"
CLASSES = {
    SENT: "sent to a model provider",
    READABLE: "readable by an agent",
    BLOCKED: "on disk but blocked",
}
ADVICE = {
    SENT: "Rotate it now: it has already left this machine.",
    READABLE: "Rotate it, or move it where no agent can read it.",
    BLOCKED: "No agent launch here can read it today.",
}
#: Bigger files are listed as not checked, which keeps memory bounded (§16).
MAX_BYTES = 64 * 1024 * 1024
SKILL_DEPTH = 4

#: Finds the secrets in one file. Kingfisher's, or a test's.
Detector = Callable[[Path], list[Match]]
#: One agent launch to check readability against, with its version.
Launch = tuple[LaunchContext, ClaudeSettings | CodexSettings, str | None]


@dataclass(frozen=True)
class Artifact:
    path: Path
    category: str
    #: Its contents went to a model provider.
    transcript: bool = False


@dataclass
class Finding:
    artifact: Artifact
    match: Match = field(repr=False)
    secret_digest: str
    path_digest: str
    exposure: str = BLOCKED
    readable_by: tuple[str, ...] = ()
    validation: str | None = None


@dataclass
class SweepReport:
    findings: list[Finding]
    scanned: int
    launches: list[str]
    not_checked: list[str]
    validated: bool = False

    def secrets(self) -> dict[str, str]:
        """Each distinct secret's digest, with the worst class it was found in."""
        order = (SENT, READABLE, BLOCKED)
        worst: dict[str, str] = {}
        for found in self.findings:
            held = worst.get(found.secret_digest)
            if held is None or order.index(found.exposure) < order.index(held):
                worst[found.secret_digest] = found.exposure
        return worst


# --- where to look ----------------------------------------------------------------


def artifacts(cwd: Path, *, home: Path, env: Mapping[str, str], platform: str) -> list[Artifact]:
    """Every file the sweep reads, each once, with its category."""
    claude = agent_paths.claude_config_dir()
    codex = agent_paths.codex_home()
    found: dict[str, Artifact] = {}

    def add(paths: Iterable[Path], category: str, *, transcript: bool = False) -> None:
        for path in paths:
            if path.is_file():
                key = os.path.normcase(os.path.abspath(path))
                found.setdefault(key, Artifact(path, category, transcript))

    add(_tree(claude / "projects", "*.jsonl"), "Claude Code transcript", transcript=True)
    add(_tree(codex / "sessions", "*.jsonl"), "Codex session", transcript=True)
    add(_tree(codex / "archived_sessions", "*.jsonl"), "Codex session", transcript=True)
    add([codex / "history.jsonl"], "Codex prompt history", transcript=True)
    instructions = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")
    add([claude / "CLAUDE.md", *(cwd / name for name in instructions)], "CLAUDE.md")
    add([codex / "AGENTS.md", cwd / "AGENTS.md"], "AGENTS.md")
    add(_skill_files(cwd), "skill")
    add([agent_paths.claude_user_config(), cwd / ".mcp.json"], "MCP config")
    add(
        [
            claude / "settings.json",
            cwd / ".claude" / "settings.json",
            cwd / ".claude" / "settings.local.json",
        ],
        "Claude Code settings",
    )
    add([codex / "config.toml"], "Codex config")
    add(_histories(home, env, platform), "shell history")
    add(dotenv_files(cwd), "project .env")
    add(_tree(cwd / ".plans", "*.md"), "flanner plan")
    add(_tree(identity.flanner_home() / "memory" / "personal", "*.md"), "flanner memory")
    add(_tree(cwd / ".flanner" / "memory", "*.md"), "flanner memory")
    return list(found.values())


def _tree(root: Path, pattern: str) -> list[Path]:
    return sorted(root.rglob(pattern)) if root.is_dir() else []


def _skill_files(cwd: Path) -> list[Path]:
    files: list[Path] = []
    for name in ("claude-code", "codex"):
        adapter = skills_adapters.adapter_for(name)
        if adapter is None:
            continue
        for root in adapter.roots(cwd):
            if not root.active:
                continue
            for skill in adapter.discover(root):
                depth = len(skill.directory.parts)
                files += [
                    path
                    for path in sorted(skill.directory.rglob("*"))
                    if path.is_file() and len(path.parts) - depth <= SKILL_DEPTH
                ]
    return files


def _histories(home: Path, env: Mapping[str, str], platform: str) -> list[Path]:
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    paths = [
        home / ".bash_history",
        home / ".zsh_history",
        data / "fish" / "fish_history",
        data / "powershell" / "PSReadLine" / "ConsoleHost_history.txt",
    ]
    if env.get("HISTFILE"):
        paths.append(Path(env["HISTFILE"]))
    if platform == "win32":
        roaming = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        powershell = roaming / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine"
        paths.append(powershell / "ConsoleHost_history.txt")
    return paths


# --- the sweep --------------------------------------------------------------------


def lower_priority() -> None:
    """Give the sweep low CPU priority (§16): it is background work."""
    with contextlib.suppress(OSError, AttributeError):
        if sys.platform == "win32":
            import ctypes

            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                kernel32 = windll.kernel32
                kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)  # below normal
        else:
            os.nice(10)


def run(
    cwd: Path,
    detector: Detector,
    launches: Sequence[Launch],
    *,
    home: Path,
    env: Mapping[str, str],
    platform: str,
    key: bytes,
) -> SweepReport:
    """Read every artifact, find its secrets, and give each finding its class."""
    found = artifacts(cwd, home=home, env=env, platform=platform)
    findings: list[Finding] = []
    big = unreadable = 0
    for artifact in found:
        try:
            if artifact.path.stat().st_size > MAX_BYTES:
                big += 1
                continue
            matches = detector(artifact.path)
        except (OSError, RuntimeError, ValueError):
            unreadable += 1
            continue
        seen: set[str] = set()
        for match in matches:
            secret = curb_store.digest(match.secret, key)
            if secret not in seen:  # a transcript repeats what it was told
                seen.add(secret)
                findings.append(
                    Finding(artifact, match, secret, curb_store.digest(str(artifact.path), key))
                )
    not_checked = []
    if big:
        not_checked.append(f"{big} file(s) over {MAX_BYTES // 2**20} MB")
    if unreadable:
        not_checked.append(f"{unreadable} file(s) that could not be read")
    labels = _classify(findings, launches, home=home, env=env, platform=platform)
    return SweepReport(findings, len(found) - big - unreadable, labels, not_checked)


def _classify(
    findings: list[Finding],
    launches: Sequence[Launch],
    *,
    home: Path,
    env: Mapping[str, str],
    platform: str,
) -> list[str]:
    paths = sorted({f.artifact.path for f in findings if not f.artifact.transcript})
    credentials = [Credential("sweep", "swept file", "Swept file", (path,)) for path in paths]
    readers: dict[Path, list[str]] = {path: [] for path in paths}
    labels = []
    for context, settings, version in launches:
        labels.append(f"{context.label}: {context.describe()}")
        report = curb_reach.assess(
            context, settings, credentials, platform=platform, home=home, env=env, version=version
        )
        for reach in report.reach:
            if reach.via:
                readers[reach.credential.paths[0]].append(context.label)
    for found in findings:
        if found.artifact.transcript:
            found.exposure = SENT
            continue
        who = tuple(dict.fromkeys(readers.get(found.artifact.path, [])))
        found.exposure = READABLE if who else BLOCKED
        found.readable_by = who
    return labels


def validate(report: SweepReport, check: Callable[[list[Match]], list[str]]) -> None:
    """Record each finding's outcome from its issuer. The caller asked the person first."""
    outcomes = check([found.match for found in report.findings])
    for found, outcome in zip(report.findings, outcomes, strict=True):
        found.validation = outcome
    report.validated = True


# --- views ------------------------------------------------------------------------


def redacted(report: SweepReport) -> dict[str, Any]:
    """Counts only: what any terminal or JSON caller may see."""
    worst = report.secrets()
    out: dict[str, Any] = {
        "secrets": len(worst),
        "by_class": {key: sum(1 for c in worst.values() if c == key) for key in CLASSES},
        "classes": CLASSES,
        "locations_by_category": dict(
            sorted(Counter(f.artifact.category for f in report.findings).items())
        ),
        "files_scanned": report.scanned,
        "launches": report.launches,
        "not_checked": report.not_checked,
    }
    if report.validated:
        out["validation"] = dict(
            sorted(Counter(str(f.validation) for f in report.findings).items())
        )
    return out


def stored(report: SweepReport) -> dict[str, Any]:
    """What is kept on disk: the counts, plus classes, rule ids and keyed digests."""
    out = redacted(report)
    out["findings"] = [
        {
            "class": f.exposure,
            "category": f.artifact.category,
            "rule": f.match.rule_id,
            "secret": f.secret_digest,
            "path": f.path_digest,
            "validation": f.validation,
        }
        for f in report.findings
    ]
    return out


def full(report: SweepReport) -> dict[str, Any]:
    """Locations and types too, for the desktop window only. Never a value."""
    out = redacted(report)
    out["findings"] = [
        {
            "class": f.exposure,
            "class_name": CLASSES[f.exposure],
            "category": f.artifact.category,
            "path": str(f.artifact.path),
            "line": f.match.line,
            "type": f.match.rule_name,
            "readable_by": list(f.readable_by),
            "validation": f.validation,
            "advice": ADVICE[f.exposure],
        }
        for f in sorted(report.findings, key=lambda f: (f.exposure, str(f.artifact.path)))
    ]
    return out
