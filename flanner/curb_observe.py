"""Observed use: what each agent has been seen using, and what could not be seen (PRD §10.7).

Evidence comes in three states:

- no evidence: the log is off, or shorter than the window, by default 14
  days and 20 sessions;
- partial evidence: the window is met, but some sessions ran without the
  hooks, so what they did is unknown;
- observed use: the window is met and every session in it was logged.

Each state comes with each channel's use, seen or not, and whether the
hooks can see that channel at all. Child processes of shell commands are
never seen, and Codex's hooks do not cover web search, MCP or apps.

"Not seen" means not seen, never "not needed". Restriction ideas are only
review candidates (guided), never applied, each listing what could not be
observed; a channel the hooks cannot see never gets one.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agent_paths, curb_fix, curb_log, curb_reach, curb_tighten
from .curb_context import CLAUDE, CODEX, LABELS

WINDOW_DAYS, WINDOW_SESSIONS = 14, 20
NO_EVIDENCE, PARTIAL, OBSERVED = "no evidence", "partial evidence", "observed use"
HOOK_MARK = " hook curb-record --agent "
EVENTS = {
    CLAUDE: ("PreToolUse", "PostToolUse", "PermissionDenied"),
    CODEX: ("PreToolUse", "PostToolUse"),
}
#: The team hooks (R5): re-check org policy at session start, and on Claude
#: Code's ConfigChange; Codex has no such event, so a file watch covers it.
SESSION_MARK = " hook curb-session --agent "
SESSION_EVENTS = {CLAUDE: ("SessionStart", "ConfigChange"), CODEX: ("SessionStart",)}
#: Which channels each agent's hooks can see a call on.
COVERAGE = {
    CLAUDE: {
        curb_reach.FILE_TOOLS: True,
        curb_reach.SHELL_FILES: True,
        curb_reach.SHELL_NETWORK: True,
        curb_reach.WEB: True,
        curb_reach.MCP: True,
    },
    CODEX: {
        curb_reach.FILE_TOOLS: True,
        curb_reach.SHELL_FILES: True,
        curb_reach.SHELL_NETWORK: True,
        curb_reach.WEB: False,
        curb_reach.MCP: False,
        curb_reach.APPS: False,
    },
}
STANDING_GAPS = {
    CLAUDE: ("child processes of shell commands",),
    CODEX: (
        "child processes of shell commands",
        "web search, MCP servers and apps, which Codex's hooks do not cover",
    ),
}


@dataclass
class Observation:
    agent: str
    state: str
    days: float
    sessions: int
    seen: dict[str, int] = field(default_factory=dict)
    covered: dict[str, bool] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    ideas: list[str] = field(default_factory=list)


# --- the hooks --------------------------------------------------------------------------------


def hook_command(agent: str, mark: str = HOOK_MARK) -> str:
    """The hook command, naming the interpreter that installs it (as messaging's does)."""
    script = (
        "import sys;sys.path[:]=[p for p in sys.path if p];from flanner.cli import main;main()"
    )
    return f'"{sys.executable}" -c "{script}"{mark}{agent}'


def _hooks_file(agent: str) -> Path:
    if agent == CLAUDE:
        return agent_paths.claude_config_dir() / "settings.json"
    return agent_paths.codex_home() / "hooks.json"


def hooks_on(agent: str, *, session: bool = False) -> bool:
    """Whether Curb's hook is installed for every event this agent logs."""
    mark, events = (SESSION_MARK, SESSION_EVENTS) if session else (HOOK_MARK, EVENTS)
    try:
        hooks = curb_tighten.load(_hooks_file(agent)).get("hooks") or {}
    except ValueError:
        return False
    return all(
        any(
            mark in str(h.get("command", ""))
            for entry in hooks.get(event, [])
            if isinstance(entry, dict)
            for h in entry.get("hooks", [])
            if isinstance(h, dict)
        )
        for event in events[agent]
    )


def hook_plan(agents: Sequence[str], *, enable: bool, session: bool = False) -> curb_fix.Plan:
    """The settings edits that add or remove Curb's hooks, for the fix machinery to apply.

    Hooks run commands, so this is never a tighten-only change: it always
    needs the person's own approval, and gets a backup and an undo.
    """
    mark, events = (SESSION_MARK, SESSION_EVENTS) if session else (HOOK_MARK, EVENTS)
    plan = curb_fix.Plan()
    for agent in agents:
        path = _hooks_file(agent)
        if agent == CODEX and not agent_paths.codex_home().exists():
            continue
        try:
            before = curb_tighten.load(path)
        except ValueError:
            plan.guided.append(f"{LABELS[agent]}'s hook file cannot be read, so it is left alone")
            continue
        after = json.loads(json.dumps(before))
        hooks = after.setdefault("hooks", {})
        for event in events[agent]:
            kept = [
                entry
                for entry in hooks.get(event, [])
                if not (
                    isinstance(entry, dict)
                    and any(
                        mark in str(h.get("command", ""))
                        for h in entry.get("hooks", [])
                        if isinstance(h, dict)
                    )
                )
            ]
            if enable:
                command = hook_command(agent, mark)
                kept.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
        if not hooks:
            after.pop("hooks", None)
        if after == before:
            continue
        if session:
            action = "re-check org policy at session start" if enable else "stop the session check"
        else:
            action = "log every tool call (metadata only)" if enable else "stop logging tool calls"
        text = json.dumps(after, indent=2) + "\n"
        plan.edits.append(curb_fix.Edit(agent, path, before, after, text, (action,)))
    return plan


# --- observed use ------------------------------------------------------------------------------


def transcript_sessions(agent: str, since: float) -> set[str]:
    """Session ids of transcripts written since a time: what ran, logged or not."""
    if agent == CLAUDE:
        root, names = agent_paths.claude_config_dir() / "projects", "*.jsonl"
    else:
        root, names = agent_paths.codex_home() / "sessions", "rollout-*.jsonl"
    found = set()
    for path in root.rglob(names) if root.is_dir() else []:
        try:
            if path.stat().st_mtime >= since:
                found.add(path.stem)
        except OSError:
            continue
    return found


def observe(
    agent: str,
    report: curb_reach.AgentReport | None = None,
    *,
    now: float | None = None,
    window_days: int = WINDOW_DAYS,
    window_sessions: int = WINDOW_SESSIONS,
) -> Observation:
    stamp = time.time() if now is None else now
    held = [r for r in curb_log.records() if r.get("kind") == "tool" and r.get("agent") == agent]
    covered = dict(COVERAGE[agent])
    seen = Counter(str(r.get("channel")) for r in held if r.get("decision") != "denied")
    observation = Observation(
        agent,
        NO_EVIDENCE,
        0.0,
        0,
        {k: seen.get(k, 0) for k in covered},
        covered,
        list(STANDING_GAPS[agent]),
    )
    if not held or not hooks_on(agent):
        observation.gaps.insert(
            0, "the action log is off" if not hooks_on(agent) else "nothing has been logged yet"
        )
        return observation
    first = min(float(r.get("time", stamp)) for r in held)
    logged = {str(r.get("session")) for r in held if r.get("session")}
    observation.days = (stamp - first) / 86400
    observation.sessions = len(logged)
    if observation.days < window_days or observation.sessions < window_sessions:
        observation.gaps.insert(
            0,
            f"the window is {window_days} days and {window_sessions} sessions; "
            f"so far {observation.days:.0f} days and {observation.sessions} sessions",
        )
        return observation
    ran = transcript_sessions(agent, first)
    unlogged = {
        s for s in ran if not any(s.endswith(x) or x.endswith(s) or x in s for x in logged)
    }
    if unlogged:
        observation.gaps.insert(0, f"{len(unlogged)} session(s) ran without the hooks")
        observation.state = PARTIAL
    else:
        observation.state = OBSERVED
    observation.ideas = _ideas(observation, report)
    return observation


def _ideas(observation: Observation, report: curb_reach.AgentReport | None) -> list[str]:
    """Review candidates: covered, open channels never seen in use. Guided, never applied."""
    open_now = (
        {
            c.key
            for c in report.channels
            if c.state in (curb_reach.UNCONTROLLED, curb_reach.UNKNOWN)
        }
        if report is not None
        else set(observation.covered)
    )
    ideas = []
    could_not = "; ".join(observation.gaps)
    for channel, covered in observation.covered.items():
        if not covered or observation.seen.get(channel) or channel not in open_now:
            continue
        ideas.append(
            f"{curb_reach.LABELS[channel]}: not seen in use in {observation.days:.0f} days and "
            f"{observation.sessions} sessions, so closing it is worth a look (not seen is not "
            f"the same as not needed; not observed: {could_not})"
        )
    return ideas


def view(observation: Observation) -> dict[str, Any]:
    return {
        "agent": observation.agent,
        "label": LABELS[observation.agent],
        "state": observation.state,
        "days": round(observation.days, 1),
        "sessions": observation.sessions,
        "channels": [
            {
                "channel": curb_reach.LABELS[key],
                "seen": count,
                "covered": observation.covered[key],
            }
            for key, count in observation.seen.items()
        ],
        "gaps": observation.gaps,
        "ideas": observation.ideas,
    }
