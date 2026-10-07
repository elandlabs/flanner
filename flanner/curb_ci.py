"""The CI check: agent steps in GitHub Actions, judged like agent launches (Curb PRD §10.12).

A step runs an agent when it uses Claude Code's, Codex's or Gemini CLI's
action, or runs one of their commands. Each such step is judged on:

    exposed   it runs on an event whose content anyone can write (issues,
              comments, pull_request_target), or such text is put into
              its prompt or environment
    power     repository secrets in its environment beyond its own API key,
              an unsafe mode, or shell commands next to a token: the one
              actions/checkout leaves in .git/config, a write-scoped
              GITHUB_TOKEN, or a step anyone can start
    egress    whether Harden-Runner blocks outbound traffic in the job

Severity, `ci-r1` version 1:

    H1 High    exposed, with power, and egress open
    M1 Medium  exposed, can run shell commands or write, and egress open
    M2 Medium  power without exposure
    L1 Low     anything else

Fixtures modelled on PromptPwnd (E1), Clinejection (E2) and Comment and
Control (E6) rate High. Output names the workflow file and line, never a
secret's name. `fix` makes the one-line edits that are safe to make blind.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .curb_sarif import Result, Rule

RULES = {
    "H1": Rule(
        "CI-H1",
        "exposed-agent-with-power",
        "Untrusted input reaches an agent step that holds secrets or a token it can use, "
        "or runs unsafe, and egress is open",
        "High",
    ),
    "M1": Rule(
        "CI-M1",
        "exposed-agent",
        "Untrusted input reaches an agent step that can run shell commands or write",
        "Medium",
    ),
    "M2": Rule(
        "CI-M2",
        "agent-with-power",
        "An agent step holds secrets or runs unsafe, though no untrusted input reaches it",
        "Medium",
    ),
    "L1": Rule("CI-L1", "agent-step", "An agent step with no untrusted input and no power", "Low"),
}

#: Events whose content people without write access can write.
UNTRUSTED_EVENTS = frozenset(
    {
        "pull_request_target",
        "issues",
        "issue_comment",
        "pull_request_review",
        "pull_request_review_comment",
        "discussion",
        "discussion_comment",
        "workflow_run",
    }
)
_ACTIONS = {
    "anthropics/claude-code-action": "Claude Code",
    "anthropics/claude-code-base-action": "Claude Code",
    "openai/codex-action": "Codex",
    "google-github-actions/run-gemini-cli": "Gemini CLI",
}
_COMMANDS = (
    (
        re.compile(r"(?:^|[\s;&|(])(?:npx\s+@anthropic-ai/claude-code|claude)\s+(?:-p|--print)\b"),
        "Claude Code",
    ),
    (re.compile(r"(?:^|[\s;&|(])(?:npx\s+@openai/codex|codex)\s+exec\b"), "Codex"),
    (
        re.compile(r"(?:^|[\s;&|(])(?:npx\s+@google/gemini-cli|gemini)\s+(?:-p|--prompt)\b"),
        "Gemini CLI",
    ),
)
#: An agent's own key: holding it is the point of the step.
_OWN_KEYS = frozenset(
    {"anthropic_api_key", "claude_code_oauth_token", "openai-api-key", "gemini_api_key"}
)
_SECRET = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}")
_EVENT_TEXT = re.compile(
    r"\$\{\{[^}]*\bgithub\.(?:head_ref|event\.(?:issue|comment|pull_request|review|discussion|"
    r"head_commit|commits)[^}]*\.(?:title|body|message|head_ref|ref|label|name))[^}]*\}\}"
)
_UNSAFE = re.compile(
    r"--dangerously-skip-permissions|bypassPermissions|--dangerously-bypass-approvals-and-sandbox"
    r"|--yolo\b|danger-full-access"
)


class _Lines(yaml.SafeLoader):
    """Records each mapping's first line, so a finding can point at its step."""


def _mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    data = loader.construct_mapping(node, deep=True)
    data["__line__"] = node.start_mark.line + 1
    return data


_Lines.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass
class Step:
    workflow: str
    job: str
    line: int
    agent: str
    name: str
    exposed_by: list[str] = field(default_factory=list)
    who: str = "write access only"
    secrets: int = 0
    unsafe: bool = False
    shell: bool = False
    token: list[str] = field(default_factory=list)
    write: bool | None = None
    egress: str = "open"
    fixes: list[str] = field(default_factory=list)

    @property
    def exposed(self) -> bool:
        return bool(self.exposed_by)

    @property
    def power(self) -> bool:
        return bool(self.secrets or self.unsafe or (self.shell and self.token))

    @property
    def rule(self) -> Rule:
        open_egress = self.egress != "blocked"
        if self.exposed and self.power and open_egress:
            return RULES["H1"]
        if self.exposed and (self.shell or self.write) and open_egress:
            return RULES["M1"]
        if self.power:
            return RULES["M2"]
        return RULES["L1"]


def _inputs(step: Mapping[str, Any]) -> Mapping[str, Any]:
    value = step.get("with")
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(f"{k} {_text(v)}" for k, v in value.items() if k != "__line__")
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return "" if value is None else str(value)


def _events(data: Mapping[Any, Any]) -> set[str]:
    raw = data.get("on", data.get(True))  # YAML 1.1 reads a bare `on` as true
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(e) for e in raw}
    if isinstance(raw, Mapping):
        return {str(k) for k in raw if k != "__line__"}
    return set()


def _writes(*scopes: Any) -> bool | None:
    """Whether the token can write: None when no permissions are set (the repo default)."""
    for scope in scopes:
        if scope is None:
            continue
        if isinstance(scope, str):
            return scope == "write-all"
        if isinstance(scope, Mapping):
            return any(v == "write" for k, v in scope.items() if k != "__line__")
    return None


def _agent(step: Mapping[str, Any]) -> str | None:
    uses = str(step.get("uses") or "")
    for prefix, label in _ACTIONS.items():
        if uses.split("@")[0] == prefix:
            return label
    run = str(step.get("run") or "")
    return next((label for pattern, label in _COMMANDS if pattern.search(run)), None)


def _tools(agent: str, step: Mapping[str, Any], text: str) -> tuple[bool, bool]:
    """Whether the step runs unsafe, and whether it can run shell commands."""
    inputs = _inputs(step)
    unsafe = bool(_UNSAFE.search(text)) or str(inputs.get("safety-strategy")) == "unsafe"
    if agent == "Codex":
        return unsafe, True  # Codex runs commands, inside its sandbox unless unsafe
    if agent == "Gemini CLI":
        return unsafe, unsafe or "run_shell_command" in text
    shell = unsafe or bool(re.search(r"allowedTools[^\n]*\bBash\b|\"Bash", text))
    return unsafe, shell


def _who(agent: str, step: Mapping[str, Any]) -> str:
    inputs = _inputs(step)
    allowed = str(inputs.get("allowed_non_write_users") or inputs.get("allow-users") or "")
    if allowed.strip() == "*":
        return "anyone"
    if allowed.strip():
        return "named users without write access too"
    if agent == "Gemini CLI" or "run" in step:
        return "anyone who can start the workflow"
    return "write access only"


def _job_facts(job: Mapping[str, Any]) -> tuple[str, bool]:
    """The job's egress control, and whether a checkout leaves its token behind."""
    egress, persists = "open", False
    for step in job.get("steps") or []:
        if not isinstance(step, Mapping):
            continue
        uses = str(step.get("uses") or "").split("@")[0]
        inputs = _inputs(step)
        if uses == "step-security/harden-runner":
            egress = "blocked" if str(inputs.get("egress-policy")) == "block" else "audited"
        if (
            uses == "actions/checkout"
            and str(inputs.get("persist-credentials")).lower() != "false"
        ):
            persists = True
    return egress, persists


def check_workflow(path: Path, root: Path) -> list[Step]:
    """Every agent step in one workflow file. Raises ValueError for one that will not parse."""
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_Lines)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as e:
        raise ValueError(f"{path.name} is not valid YAML: {e}") from None
    if not isinstance(data, Mapping):
        return []
    relative = path.relative_to(root).as_posix()
    events = _events(data)
    untrusted = sorted(events & UNTRUSTED_EVENTS)
    found = []
    held_jobs = data.get("jobs")
    jobs: Mapping[Any, Any] = held_jobs if isinstance(held_jobs, Mapping) else {}
    for job_name, job in jobs.items():
        if job_name == "__line__" or not isinstance(job, Mapping):
            continue
        egress, persists = _job_facts(job)
        writes = _writes(job.get("permissions"), data.get("permissions"))
        for raw in job.get("steps") or []:
            if not isinstance(raw, Mapping):
                continue
            agent = _agent(raw)
            if agent is None:
                continue
            text = _text(raw)
            step = Step(
                relative,
                str(job_name),
                int(raw.get("__line__", 1)),
                agent,
                str(raw.get("name") or raw.get("id") or agent),
            )
            step.exposed_by = list(untrusted)
            if _EVENT_TEXT.search(text):
                step.exposed_by.append("event text in its prompt or environment")
            step.who = _who(agent, raw)
            inputs = _inputs(raw)
            held = {
                *_SECRET.findall(_text({k: v for k, v in inputs.items() if k not in _OWN_KEYS})),
                *_SECRET.findall(_text(raw.get("env"))),
                *_SECRET.findall(_text(job.get("env"))),
                *_SECRET.findall(_text(data.get("env"))),
            }
            step.secrets = len(held)
            step.unsafe, step.shell = _tools(agent, raw, text)
            step.write = writes
            if persists:
                step.token.append("the token actions/checkout leaves in .git/config")
            if writes:
                step.token.append("a workflow token that can write")
            if step.who == "anyone":
                step.token.append("a runner anyone can drive")
            step.egress = egress
            step.fixes = _advice(step)
            found.append(step)
    return found


def _advice(step: Step) -> list[str]:
    out = []
    if step.who == "anyone":
        out.append("remove allowed_non_write_users, so only people with write access start it")
    if step.unsafe:
        out.append(
            "drop the unsafe mode: a bypass flag, --yolo, danger-full-access or "
            "safety-strategy unsafe"
        )
    if step.secrets:
        out.append("move repository secrets out of the agent step's environment")
    if any("checkout" in t for t in step.token):
        out.append("set persist-credentials: false on actions/checkout")
    if step.egress != "blocked":
        out.append("add step-security/harden-runner with egress-policy: block")
    if any("event text" in e for e in step.exposed_by):
        out.append("keep issue, comment and pull request text out of the prompt")
    return out


def check(root: Path) -> tuple[list[Step], list[str]]:
    """Every agent step in a repository's workflows, and the files that could not be read."""
    folder = root / ".github" / "workflows"
    steps: list[Step] = []
    problems: list[str] = []
    for path in sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]) if folder.is_dir() else []:
        try:
            steps += check_workflow(path, root)
        except (ValueError, OSError) as e:
            problems.append(str(e))
    return steps, problems


def message(step: Step) -> str:
    facts = [f"{step.agent} step '{step.name}' in job '{step.job}'"]
    facts.append(
        "untrusted input from " + ", ".join(step.exposed_by)
        if step.exposed
        else "no untrusted input"
    )
    facts.append(f"started by {step.who}")
    if step.secrets:
        facts.append(f"{step.secrets} repository secret(s) in its environment")
    if step.unsafe:
        facts.append("an unsafe mode")
    if step.shell:
        facts.append("shell commands allowed")
    facts += step.token
    facts.append(f"egress {step.egress}")
    return "; ".join(facts) + "."


def results(steps: Sequence[Step]) -> list[Result]:
    return [
        Result(
            step.rule,
            message(step) + (" Fix: " + "; ".join(step.fixes) + "." if step.fixes else ""),
            step.workflow,
            step.line,
            {
                "severity": step.rule.severity,
                "disposition": "guided",
                "evidence": "configured" if step.write is not None else "assumed",
                "agent": step.agent,
            },
        )
        for step in steps
    ]


_FIXES = (
    (
        re.compile(r"^\s*allowed_non_write_users\s*:.*\n?", re.M),
        "",
        "only people with write access start it",
    ),
    (
        re.compile(r"(safety-strategy\s*:\s*)[\"']?unsafe[\"']?"),
        r"\1drop-sudo",
        "Codex drops sudo",
    ),
    (
        re.compile(r"(sandbox\s*:\s*)[\"']?danger-full-access[\"']?"),
        r"\1workspace-write",
        "Codex keeps its sandbox",
    ),
    (re.compile(r"[ \t]*--dangerously-skip-permissions\b"), "", "Claude Code asks again"),
    (re.compile(r"[ \t]*--yolo\b"), "", "no --yolo"),
)


def fix(path: Path, *, write: bool = True) -> list[str]:
    """Make the one-line fixes that are safe without a person, if the file still parses.

    With `write` off, only says what it would do.
    """
    original = path.read_text(encoding="utf-8")
    text, done = original, []
    for pattern, replacement, said in _FIXES:
        text, count = pattern.subn(replacement, text)
        if count:
            done.append(said)
    if text == original:
        return []
    try:
        yaml.load(text, Loader=_Lines)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError:
        return []
    if write:
        path.write_text(text, encoding="utf-8")
    return done
