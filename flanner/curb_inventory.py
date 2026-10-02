"""Which agents are on this machine and in this repo, and what each loads (Curb PRD §10.1).

Read-only. Two things here run a program, and both through an injected
runner so tests never touch the real machine: `<agent> --version`, to place
each agent on the support matrix, and the schedulers' own listing commands
(`crontab -l`, `schtasks /query`), to find jobs that run an agent
unattended. Nothing a repository contains is ever executed.

A scheduled job is assessed in its own launch context, taken from its
definition, because an unattended run with a bypass flag is the riskiest
launch a machine has.
"""

from __future__ import annotations

import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from . import agent_paths, skills_adapters
from .curb_context import AGENTS, BASELINE, CLAUDE, CODEX, LABELS, LaunchContext, LaunchError
from .curb_context import parse as parse_launch
from .curb_settings import ClaudeSettings, CodexSettings, HookSet, Layer, McpServer

#: Runs a command and returns its standard output, or None if it failed.
Runner = Callable[[Sequence[str]], "str | None"]

_VERSION = re.compile(r"(\d+\.\d+\.\d+)")
_SKILL_AGENT = {CLAUDE: "claude-code", CODEX: "codex"}
_TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def run(argv: Sequence[str]) -> str | None:
    """The real runner: a short, quiet subprocess with no shell."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, never from repository content
            list(argv), capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    scheduler: str
    #: The command line, for the full view only: a prompt can hold anything.
    command: tuple[str, ...]
    context: LaunchContext | None
    problem: str | None = None


@dataclass
class AgentInventory:
    agent: str
    installed: bool
    binary: str | None
    version: str | None
    supported: bool
    config_present: bool
    layers: list[Layer] = field(default_factory=list)
    mcp: list[McpServer] = field(default_factory=list)
    hooks: list[HookSet] = field(default_factory=list)
    skills: dict[str, int] = field(default_factory=dict)
    jobs: list[ScheduledJob] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABELS[self.agent]

    @property
    def seen(self) -> bool:
        return self.installed or self.config_present or bool(self.jobs)


def version_of(agent: str, runner: Runner | None = None) -> tuple[str | None, str | None]:
    """The agent's program and the version it reports, either None if not found."""
    binary = shutil.which(agent)
    if binary is None:
        return None, None
    out = (runner or run)([binary, "--version"]) or ""
    match = _VERSION.search(out)
    return binary, match.group(1) if match else None


def skills(agent: str, project: Path | None) -> dict[str, int]:
    """How many skill packages the agent would find, by scope."""
    adapter = skills_adapters.adapter_for(_SKILL_AGENT[agent])
    if adapter is None:
        return {}
    counts: dict[str, int] = {}
    for root in adapter.roots(project):
        if not root.active:
            continue
        found = adapter.discover(root)
        if found:
            counts[root.scope] = counts.get(root.scope, 0) + len(found)
    return counts


def gather(
    agent: str,
    settings: ClaudeSettings | CodexSettings,
    *,
    project: Path | None,
    jobs: list[ScheduledJob],
    runner: Runner | None = None,
) -> AgentInventory:
    binary, version = version_of(agent, runner)
    config = agent_paths.claude_config_dir() if agent == CLAUDE else agent_paths.codex_home()
    return AgentInventory(
        agent=agent,
        installed=binary is not None,
        binary=binary,
        version=version,
        supported=version == BASELINE[agent],
        config_present=config.is_dir(),
        layers=list(settings.layers),
        mcp=list(settings.mcp),
        hooks=list(settings.hooks),
        skills=skills(agent, project),
        jobs=[job for job in jobs if job.context is not None and job.context.agent == agent],
    )


# --- scheduled jobs -------------------------------------------------------------


def is_agent_run(argv: Sequence[str]) -> bool:
    """`claude -p ...` or `codex exec ...`, anywhere in a command line.

    A job usually wraps the agent in a shell (`bash -lc "cd x && claude -p"`),
    so the words are searched rather than only the first one.
    """
    words = [w for w in argv]
    for index, word in enumerate(words):
        name = Path(word.replace("\\", "/")).name.lower().removesuffix(".exe").removesuffix(".cmd")
        rest = words[index + 1 :]
        if name == CLAUDE and any(
            w in ("-p", "--print") or w.startswith("--print=") for w in rest
        ):
            return True
        if name == CODEX and rest[:1] and rest[0] in ("exec", "e"):
            return True
    return False


def _agent_argv(argv: Sequence[str]) -> list[str]:
    """The agent's own words out of a wrapped command line."""
    words = list(argv)
    for index, word in enumerate(words):
        if Path(word.replace("\\", "/")).name.lower().split(".")[0] in AGENTS:
            tail = []
            for later in words[index:]:
                if later in ("&&", "||", ";", "|"):
                    break
                tail.append(later)
            return tail
    return words


def _split_command(text: str) -> list[str]:
    try:
        words = shlex.split(text, posix=True)
    except ValueError:
        words = text.split()
    # A wrapped `bash -c "..."` keeps its script in one word; open it up.
    expanded: list[str] = []
    for word in words:
        if " " in word and any(agent in word for agent in AGENTS):
            expanded.extend(_split_command(word))
        else:
            expanded.append(word)
    return expanded


def _job(name: str, scheduler: str, argv: Sequence[str], cwd: Path) -> ScheduledJob | None:
    if not is_agent_run(argv):
        return None
    words = _agent_argv(argv)
    try:
        context = parse_launch(words, cwd, source=f"scheduled job: {name}")
    except LaunchError as error:
        return ScheduledJob(name, scheduler, tuple(argv), None, str(error))
    return ScheduledJob(name, scheduler, tuple(argv), context)


def scheduled_jobs(
    home: Path, *, platform: str | None = None, runner: Runner | None = None
) -> list[ScheduledJob]:
    """Jobs on this machine that run an agent unattended."""
    platform = platform or sys.platform
    runner = runner or run
    found: list[ScheduledJob] = []
    if platform == "win32":
        found += _windows_tasks(home, runner)
    else:
        found += _crontab(home, runner)
        if platform == "darwin":
            found += _launchd(home)
        else:
            found += _systemd(home)
    return found


def _crontab(home: Path, runner: Runner) -> list[ScheduledJob]:
    out = runner(["crontab", "-l"]) or ""
    jobs = []
    for number, line in enumerate(out.splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#") or "=" in text.split()[0]:
            continue
        fields = text.split(None, 1 if text.startswith("@") else 5)
        command = fields[-1] if len(fields) > 1 else ""
        job = _job(f"crontab line {number}", "cron", _split_command(command), home)
        if job:
            jobs.append(job)
    return jobs


def _launchd(home: Path) -> list[ScheduledJob]:
    jobs = []
    for folder in (home / "Library" / "LaunchAgents",):
        for path in sorted(folder.glob("*.plist")) if folder.is_dir() else []:
            try:
                data: Any = plistlib.loads(path.read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            argv = data.get("ProgramArguments") if isinstance(data, dict) else None
            if not isinstance(argv, list):
                program = data.get("Program") if isinstance(data, dict) else None
                argv = [program] if isinstance(program, str) else []
            argv = _expand([str(a) for a in argv])
            cwd = Path(data.get("WorkingDirectory") or home) if isinstance(data, dict) else home
            job = _job(path.stem, "launchd", argv, cwd)
            if job:
                jobs.append(job)
    return jobs


def _expand(argv: list[str]) -> list[str]:
    out: list[str] = []
    for word in argv:
        out.extend(_split_command(word) if " " in word else [word])
    return out


def _systemd(home: Path) -> list[ScheduledJob]:
    jobs = []
    folder = home / ".config" / "systemd" / "user"
    for path in sorted(folder.glob("*.service")) if folder.is_dir() else []:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        cwd = home
        command = ""
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip() == "ExecStart":
                command = value.strip().lstrip("-@+!:")
            elif key.strip() == "WorkingDirectory":
                cwd = Path(value.strip().replace("%h", str(home)))
        timer = path.with_suffix(".timer")
        if command and timer.is_file():
            job = _job(path.stem, "systemd timer", _split_command(command), cwd)
            if job:
                jobs.append(job)
    return jobs


def _windows_tasks(home: Path, runner: Runner) -> list[ScheduledJob]:
    # One XML document of every task: a tenth of the time of `/fo CSV /v`.
    out = runner(["schtasks", "/query", "/xml", "ONE"]) or ""
    try:
        # noqa S314: the scheduler's own serialisation, which carries no DTD.
        tasks = ElementTree.fromstring(out) if out.strip() else None  # noqa: S314
    except ElementTree.ParseError:
        return []
    jobs = []
    for task in tasks.findall("t:Task", _TASK_NS) if tasks is not None else []:
        name = (task.findtext("t:RegistrationInfo/t:URI", "", _TASK_NS) or "").lstrip("\\")
        for action in task.findall("t:Actions/t:Exec", _TASK_NS):
            program = (action.findtext("t:Command", "", _TASK_NS) or "").strip().strip('"')
            arguments = action.findtext("t:Arguments", "", _TASK_NS) or ""
            folder = (action.findtext("t:WorkingDirectory", "", _TASK_NS) or "").strip()
            argv = [program, *_split_command(arguments)]
            job = _job(name, "Task Scheduler", argv, Path(folder) if folder else home)
            if job:
                jobs.append(job)
    return jobs


# --- views --------------------------------------------------------------------


def redacted(inventory: AgentInventory) -> dict[str, Any]:
    """Names of agents, servers, hooks and jobs; credential names only as counts."""
    return {
        "agent": inventory.agent,
        "label": inventory.label,
        "installed": inventory.installed,
        "version": inventory.version,
        "supported": inventory.supported,
        "baseline": BASELINE[inventory.agent],
        "settings_layers": [
            {
                "name": layer.name,
                "controlled_by": layer.controller,
                "present": layer.present,
                "problem": layer.error,
            }
            for layer in inventory.layers
        ],
        "mcp_servers": [
            {
                "name": s.name,
                "transport": s.transport,
                "configured_in": s.source,
                "controlled_by": s.controller,
                "env_vars": len(s.env_names),
            }
            for s in inventory.mcp
        ],
        "hooks": [
            {
                "event": h.event,
                "count": h.count,
                "configured_in": h.source,
                "controlled_by": h.controller,
            }
            for h in inventory.hooks
        ],
        "skills": inventory.skills,
        "scheduled_jobs": [
            {
                "name": j.name,
                "scheduler": j.scheduler,
                "launch": j.context.describe() if j.context else None,
                "problem": j.problem,
            }
            for j in inventory.jobs
        ],
    }


def full(inventory: AgentInventory) -> dict[str, Any]:
    out = redacted(inventory)
    out["binary"] = inventory.binary
    for row, layer in zip(out["settings_layers"], inventory.layers, strict=True):
        row["where"] = layer.where
    for row, server in zip(out["mcp_servers"], inventory.mcp, strict=True):
        row["command"] = list(server.command[:1])
        row["env_names"] = list(server.env_names)
    for row, job in zip(out["scheduled_jobs"], inventory.jobs, strict=True):
        row["command"] = list(job.command)
    return out
