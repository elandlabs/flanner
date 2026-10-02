"""The R1 reach-now corpus, scored per stratum (Curb PRD §15, §16).

A case is one launch posture, one set of planted credentials, MCP on or off,
on one operating system. Its labels are the posture's hand-written
expectations below, taken from each agent's documentation (E numbers are the
PRD's evidence list), and the severity-r1 table in PRD §9.4. Curb's code
computes none of them, so a disagreement is a bug in one or the other.

A stratum is agent × OS × channel × expected severity. The gates:

- every stratum has at least 20 cases;
- in each, recall and precision of "open" (uncontrolled or unknown) are at
  least 95%;
- at least 98% of each agent's cases match on every channel and the severity;
- the critical invariants hold on every case.

Each OS is applied as its documented rules (the platform Curb is told it
runs on), so every OS stratum is measured on any test machine.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from flanner import curb_credentials, curb_reach, curb_settings
from flanner.curb_context import BASELINE, CLAUDE, CODEX, default, parse
from flanner.curb_reach import (
    APPS,
    FILE_TOOLS,
    MCP,
    SHELL_FILES,
    SHELL_NETWORK,
    WEB,
)

C, U, A, K = "controlled", "uncontrolled", "absent", "unknown"
OPEN = {U, K}
OSES = ("darwin", "linux", "win32")
SEVERITIES = ("High", "Medium", "Low")
CHANNELS = {
    CLAUDE: (FILE_TOOLS, SHELL_FILES, SHELL_NETWORK, WEB, MCP),
    CODEX: (FILE_TOOLS, SHELL_FILES, SHELL_NETWORK, WEB, MCP, APPS),
}
MIN_CASES, MIN_RATE, MIN_AGREEMENT = 20, 0.95, 0.98

# Planted credentials. Every one is wide (§9.4): cloud, SSH and Kubernetes
# credentials by kind, and the rest because their scope can't be read.
AWS, SSH, KUBE, DOTENV, ENV, AGENT = "aws", "ssh", "kube", "dotenv", "env", "agent"
FILES = frozenset({AWS, SSH, KUBE, DOTENV})
PROFILES = tuple(
    frozenset(p)
    for p in ((), {AWS}, {SSH}, {KUBE}, {DOTENV}, {ENV}, {AGENT}, {AWS, DOTENV, ENV}, {KUBE, ENV})
)
SECRET_ENV = "OPENAI_API_KEY"

Content = Any  # dict (JSON), str, a callable taking the workspace, or None


@dataclass(frozen=True)
class Posture:
    """One launch: its files, its flags, and what the documentation says it does."""

    name: str
    argv: tuple[str, ...] | Callable[[SimpleNamespace], tuple[str, ...]] = ()
    #: ~/.claude/settings.json, or ~/.codex/config.toml.
    user: Content = None
    #: .claude/settings.json, or .codex/config.toml, in the project.
    project: Content = None
    #: managed-settings.json, or /etc/codex/requirements.toml.
    managed: Content = None
    #: More files in the agent's own folder, by name.
    files: Mapping[str, Content] = field(default_factory=dict)
    # The labels.
    read: bool = True  # Claude Code's Read tool is available
    shell: bool = True  # shell tools are available
    read_denied: frozenset[str] = frozenset()  # files the Read tool may not open
    proc_denied: bool = False  # Read(/proc/self/environ) is denied (E3)
    blocked: frozenset[str] = frozenset()  # files shell commands can't read
    env_hidden: bool = False  # the secret variable never reaches commands
    net: str = U
    web: str = U
    external: bool | None = None  # outside content arrives; by default, web == U
    mcp_kept: bool = True  # a configured MCP server loads
    apps: str = A  # Codex only
    doubt: bool = False  # an unknown flag or an unreadable setting
    win32: Mapping[str, Any] = field(default_factory=dict)  # labels that differ on Windows


# --- Claude Code postures (E3, E11–E14) ------------------------------------------

READ_DENIES = ["Read(~/.aws/**)", "Read(~/.ssh/**)", "Read(./.env)", "Read(//proc/self/environ)"]
DENIED = frozenset({AWS, SSH, DOTENV})
WEB_OFF = ["WebFetch", "WebSearch"]
NO_SHELL = "Bash,Monitor,PowerShell"


def _dotenvs(w: SimpleNamespace) -> list[str]:
    return [str(w.plain / ".env"), str(w.dotenv / ".env")]


def _hard(
    w: SimpleNamespace,
    *,
    escape: bool = False,
    domains: tuple[str, ...] = ("pypi.org",),
    excluded: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Every Claude Code control on: strict sandbox, denials, allowlist, web off."""
    sandbox: dict[str, Any] = {
        "enabled": True,
        "allowUnsandboxedCommands": escape,
        "filesystem": {"denyRead": ["~/.aws", "~/.ssh", *_dotenvs(w)]},
        "credentials": {"envVars": [{"name": SECRET_ENV, "mode": "deny"}]},
        "network": {"strictAllowlist": True, "allowedDomains": list(domains)},
    }
    if excluded:
        sandbox["excludedCommands"] = list(excluded)
    return {"sandbox": sandbox, "permissions": {"deny": [*READ_DENIES, *WEB_OFF]}}


HARD = {
    "read_denied": DENIED,
    "proc_denied": True,
    "blocked": DENIED,
    "env_hidden": True,
    "net": C,
    "web": C,
}


def _allowlist(
    w: SimpleNamespace, *, escape: bool = False, mode: str | None = None
) -> dict[str, Any]:
    """A sandbox with an allowlist that is not strict, and the web tools denied."""
    out: dict[str, Any] = {
        "sandbox": {
            "enabled": True,
            "allowUnsandboxedCommands": escape,
            "filesystem": {"denyRead": ["~/.aws"]},
            "network": {"allowedDomains": ["pypi.org"]},
        },
        "permissions": {"deny": list(WEB_OFF)},
    }
    if mode:
        out["permissions"]["defaultMode"] = mode
    return out


ONE = frozenset({AWS})

CLAUDE_POSTURES = (
    Posture("no settings"),
    Posture(
        "Read deny rules only (E13)",
        user={"permissions": {"deny": READ_DENIES}},
        read_denied=DENIED,
        proc_denied=True,
    ),
    Posture("web tools denied", user={"permissions": {"deny": WEB_OFF}}, web=C),
    Posture("only WebFetch denied", user={"permissions": {"deny": ["WebFetch"]}}),
    Posture(
        "every fetch domain denied",
        user={"permissions": {"deny": ["WebFetch(domain:*)", "WebSearch"]}},
        web=C,
    ),
    Posture("sandbox on, escape hatch open", user={"sandbox": {"enabled": True}}),
    Posture(
        "strict sandbox, nothing denied",
        user={"sandbox": {"enabled": True, "allowUnsandboxedCommands": False}},
    ),
    Posture(
        "strict sandbox denying two folders",
        user={
            "sandbox": {
                "enabled": True,
                "allowUnsandboxedCommands": False,
                "filesystem": {"denyRead": ["~/.aws", "~/.ssh"]},
            }
        },
        blocked=frozenset({AWS, SSH}),
    ),
    Posture("hardened", user=_hard, **HARD),
    Posture(
        "hardened, allowlist admits every host",
        user=lambda w: _hard(w, domains=("*",)),
        **{**HARD, "net": U},
    ),
    Posture(
        "hardened, escape hatch open",
        user=lambda w: _hard(w, escape=True),
        read_denied=DENIED,
        proc_denied=True,
        web=C,
    ),
    Posture(
        "hardened, one command excluded",
        user=lambda w: _hard(w, excluded=("docker",)),
        read_denied=DENIED,
        proc_denied=True,
        web=C,
    ),
    Posture(
        "hardened, launched skipping permissions",
        user=_hard,
        argv=("--dangerously-skip-permissions",),
        **HARD,
    ),
    Posture("allowlist, not strict", user=_allowlist, blocked=ONE, web=C),
    Posture(
        "allowlist, launched skipping permissions",
        user=_allowlist,
        argv=("--dangerously-skip-permissions",),
        blocked=ONE,
        web=C,
    ),
    Posture(
        "allowlist, escape hatch open, prompts refused at launch",
        user=lambda w: _allowlist(w, escape=True),
        argv=("--permission-prompts", "none"),
        blocked=ONE,
        net=C,
        web=C,
    ),
    Posture(
        "allowlist, escape hatch open, dontAsk mode",
        user=lambda w: _allowlist(w, escape=True, mode="dontAsk"),
        blocked=ONE,
        net=C,
        web=C,
    ),
    Posture(
        "allowlist, bypass mode from user settings",
        user=lambda w: _allowlist(w, mode="bypassPermissions"),
        blocked=ONE,
        web=C,
    ),
    Posture(
        "allowlist, escape hatch open, dontAsk from the project",
        user=lambda w: _allowlist(w, escape=True),
        project={"permissions": {"defaultMode": "dontAsk"}},
        blocked=ONE,
        net=C,
        web=C,
    ),
    Posture(
        "allowlist, bypass mode from the project is ignored (E11)",
        user=_allowlist,
        project={"permissions": {"defaultMode": "bypassPermissions"}},
        argv=("--permission-prompts", "none"),
        blocked=ONE,
        net=C,
        web=C,
    ),
    Posture(
        "managed domains only, launched skipping permissions",
        managed={
            "sandbox": {
                "enabled": True,
                "allowUnsandboxedCommands": False,
                "network": {"allowManagedDomainsOnly": True, "allowedDomains": ["pypi.org"]},
            }
        },
        user={"permissions": {"deny": WEB_OFF}},
        argv=("--dangerously-skip-permissions",),
        net=C,
        web=C,
    ),
    Posture("shell tools disallowed at launch", argv=("--disallowedTools", NO_SHELL), shell=False),
    Posture("only Read and Edit at launch", argv=("--tools", "Read,Edit"), shell=False, web=C),
    Posture("only Read at launch", argv=("--tools", "Read"), shell=False, web=C),
    Posture("Read, Grep and Glob only", argv=("--tools", "Read,Grep,Glob"), shell=False, web=C),
    Posture(
        "shell disallowed at launch, Read deny rules",
        user={"permissions": {"deny": READ_DENIES}},
        argv=("--disallowedTools", NO_SHELL),
        shell=False,
        read_denied=DENIED,
        proc_denied=True,
    ),
    Posture(
        "shell and web tools denied in settings",
        user={"permissions": {"deny": ["Bash", "Monitor", "PowerShell", *WEB_OFF]}},
        shell=False,
        web=C,
    ),
    Posture(
        "shell and web tools disallowed at launch",
        argv=("--disallowedTools", f"{NO_SHELL},WebFetch,WebSearch"),
        shell=False,
        web=C,
    ),
    Posture(
        "hardened settings file given at launch",
        files={"ci.json": _hard},
        argv=lambda w: ("--settings", str(w.claude / "ci.json")),
        **HARD,
    ),
    Posture(
        "hardened user settings not loaded by the launch",
        user=_hard,
        argv=("--setting-sources", "project"),
    ),
    Posture("strict MCP config at launch", argv=("--strict-mcp-config",), mcp_kept=False),
    Posture(
        "MCP server denied by managed settings",
        managed={"deniedMcpServers": [{"serverName": "pencil"}]},
        mcp_kept=False,
    ),
    Posture("unknown launch flag", argv=("--frobnicate",), doubt=True),
    Posture("hardened, unknown launch flag", user=_hard, argv=("--frobnicate",), doubt=True),
    Posture(
        "hardened settings cut off mid-file",
        user=lambda w: json.dumps(_hard(w))[:-1],
        doubt=True,
    ),
)


# --- Codex postures (E10, E16, E47) -----------------------------------------------
#
# A permissions profile (`default_permissions`) replaces `sandbox_mode` and does
# not combine with it. Outside its workspace roots a profile reads only what it
# lists, the most specific entry winning, `deny` over `write` over `read`.


def _toml_string(text: str) -> str:
    return json.dumps(text)  # a JSON string is a valid TOML basic string


def _codex(
    w: SimpleNamespace,
    *,
    profile: bool = True,
    sandbox: str | None = None,
    approval: str | None = '"never"',
    web: str | None = '"disabled"',
    top: tuple[str, ...] = (),
    features: tuple[str, ...] = ("apps = false",),
    home: bool = True,
    deny: bool = True,
    roots: tuple[str, ...] = (),
    parent: bool = True,
    network: str | None = None,
    domains: tuple[str, ...] = (),
    tables: tuple[str, ...] = (),
    trust: bool = False,
) -> str:
    """A strict Codex config: a profile on `:workspace` that reads home but its secrets.

    `home` grants the home folder, so the profile reads as widely as the older
    sandbox modes do, and `deny` takes the credential folders and `.env`
    files back out. `profile=False` writes the older `sandbox_mode` instead.
    """
    lines = [] if approval is None else [f"approval_policy = {approval}"]
    if web is not None:
        lines.append(f"web_search = {web}")
    if sandbox is not None:
        lines.append(f"sandbox_mode = {sandbox}")
    if profile:
        lines.append('default_permissions = "locked"')
    lines += list(top)
    if features:
        lines += ["[features]", *features]
    if profile:
        lines += [
            "[permissions.locked]",
            'extends = ":workspace"' if parent else 'description = "x"',
        ]
        entries = ['"~" = "read"'] if home else []
        if deny:
            entries += ['"~/.aws" = "deny"', '"~/.ssh" = "deny"']
            entries += [f'{_toml_string(path)} = "deny"' for path in _dotenvs(w)]
        if entries:
            lines += ["[permissions.locked.filesystem]", *entries]
        if roots:
            lines += ['[permissions.locked.filesystem.":workspace_roots"]', *roots]
        if network is not None:
            lines += ["[permissions.locked.network]", f"enabled = {network}"]
        if domains:
            lines += ["[permissions.locked.network.domains]", *domains]
    lines += list(tables)
    if trust:
        for project in (w.plain, w.dotenv):
            lines += [f"[projects.{_toml_string(str(project))}]", 'trust_level = "trusted"']
    return "\n".join(lines) + "\n"


STRICT = {"blocked": DENIED, "net": C, "web": C}
FULL = {"net": U, "web": C}
HOME_FILES = frozenset({AWS, SSH, KUBE})
RISKY_PROJECT = 'default_permissions = ":danger-full-access"\nweb_search = "live"\n'
PROXY = ("apps = false", "network_proxy = true")

CODEX_POSTURES = (
    Posture("no config", web=C, external=True, apps=K),
    Posture("strict profile", user=_codex, **STRICT),
    Posture("strict profile without denials", user=lambda w: _codex(w, deny=False), net=C, web=C),
    Posture(
        "strict profile, network on",
        user=lambda w: _codex(w, network="true"),
        **{**STRICT, "net": U},
    ),
    Posture(
        "strict profile, network through the proxy's allowlist",
        user=lambda w: _codex(
            w, features=PROXY, network="true", domains=('"pypi.org" = "allow"',)
        ),
        **STRICT,
    ),
    Posture(
        "strict profile, proxy allows every domain",
        user=lambda w: _codex(w, features=PROXY, network="true", domains=('"*" = "allow"',)),
        **{**STRICT, "net": U},
    ),
    Posture(
        "full access, older sandbox setting",
        user=lambda w: _codex(w, profile=False, sandbox='"danger-full-access"'),
        **FULL,
    ),
    Posture(
        "strict profile, but approvals on request",
        user=lambda w: _codex(w, approval='"on-request"'),
        **FULL,
    ),
    Posture(
        "read-only, older sandbox setting",
        user=lambda w: _codex(w, profile=False, sandbox='"read-only"'),
        net=C,
        web=C,
    ),
    Posture(
        "workspace-write, older sandbox setting",
        user=lambda w: _codex(w, profile=False, sandbox='"workspace-write"'),
        net=C,
        web=C,
    ),
    Posture(
        "strict profile, automatic approval reviewer",
        user=lambda w: _codex(w, top=('approvals_reviewer = "auto_review"',)),
        **FULL,
    ),
    Posture(
        "strict profile, live web search",
        user=lambda w: _codex(w, web='"live"'),
        **{**STRICT, "web": U},
    ),
    Posture(
        "strict profile, cached web search",
        user=lambda w: _codex(w, web='"cached"'),
        external=True,
        **STRICT,
    ),
    Posture(
        "strict profile, one app enabled",
        user=lambda w: _codex(w, features=(), tables=("[apps.github]", "enabled = true")),
        apps=U,
        **STRICT,
    ),
    Posture(
        "strict profile, apps off by default",
        user=lambda w: _codex(w, features=(), tables=("[apps._default]", "enabled = false")),
        **STRICT,
    ),
    Posture(
        "strict profile, apps left at their default",
        user=lambda w: _codex(w, features=()),
        apps=K,
        **STRICT,
    ),
    Posture(
        "strict profile, core environment only",
        user=lambda w: _codex(w, tables=("[shell_environment_policy]", 'inherit = "core"')),
        env_hidden=True,
        **STRICT,
    ),
    Posture(
        "strict profile, default secret excludes on",
        user=lambda w: _codex(
            w, tables=("[shell_environment_policy]", "ignore_default_excludes = false")
        ),
        env_hidden=True,
        **STRICT,
    ),
    Posture(
        "strict profile, the secret variable excluded by pattern",
        user=lambda w: _codex(w, tables=("[shell_environment_policy]", 'exclude = ["OPENAI_*"]')),
        env_hidden=True,
        **STRICT,
    ),
    Posture(
        "strict profile, launched bypassing approvals and sandbox",
        user=_codex,
        argv=("--dangerously-bypass-approvals-and-sandbox",),
        **FULL,
    ),
    Posture(
        "strict profile, sandbox overridden at launch",
        user=_codex,
        argv=("-s", "danger-full-access"),
        **FULL,
    ),
    Posture(
        "strict profile, web search turned on at launch",
        user=_codex,
        argv=("--search",),
        **{**STRICT, "web": U},
    ),
    Posture(
        "strict profile, a full-access config profile picked at launch",
        user=_codex,
        files={"yolo.config.toml": 'default_permissions = ":danger-full-access"\n'},
        argv=("-p", "yolo"),
        **FULL,
    ),
    Posture(
        "strict profile, a legacy config profile with live search",
        user=lambda w: _codex(w, tables=("[profiles.calm]", 'web_search = "live"')),
        argv=("-p", "calm"),
        **{**STRICT, "web": U},
    ),
    Posture(
        "strict profile, an untrusted project asks for full access (E47)",
        user=_codex,
        project=RISKY_PROJECT,
        **STRICT,
    ),
    Posture(
        "strict profile, a trusted project asks for full access",
        user=lambda w: _codex(w, trust=True),
        project=RISKY_PROJECT,
        net=U,
        web=U,
    ),
    Posture(
        "strict profile, network turned on with -c",
        user=_codex,
        argv=("-c", "permissions.locked.network.enabled=true"),
        **{**STRICT, "net": U},
    ),
    Posture(
        "strict profile, the admin disables the MCP server",
        user=_codex,
        managed='disabled_mcp_servers = ["pencil"]\n',
        mcp_kept=False,
        win32={"mcp_kept": True},  # /etc/codex is not read on Windows
        **STRICT,
    ),
    Posture(
        "deny_read from the admin only",
        user=lambda w: _codex(w, deny=False),
        managed='[permissions.filesystem]\ndeny_read = ["~/.aws", "~/.ssh"]\n',
        blocked=frozenset({AWS, SSH}),
        net=C,
        web=C,
        win32={"blocked": frozenset()},
    ),
    Posture(
        "strict profile, granular approvals that never ask",
        user=lambda w: _codex(w, approval="{ granular = { sandbox_approval = false } }"),
        **STRICT,
    ),
    Posture(
        "strict profile, apps turned off at launch",
        user=lambda w: _codex(w, features=()),
        argv=("--disable", "apps"),
        **STRICT,
    ),
    Posture(
        "profile that grants nothing outside the workspace",
        user=lambda w: _codex(w, home=False, deny=False),
        blocked=HOME_FILES,
        net=C,
        web=C,
    ),
    Posture(
        "profile denying .env files in the workspace",
        user=lambda w: _codex(w, home=False, deny=False, roots=('"**/.env" = "deny"',)),
        blocked=HOME_FILES | {DOTENV},
        net=C,
        web=C,
    ),
    Posture(
        "profile with no parent and no entries",
        user=lambda w: _codex(w, home=False, deny=False, parent=False),
        blocked=FILES,
        net=C,
        web=C,
    ),
    Posture(
        "profile and sandbox_mode together, which Codex says do not combine",
        user=lambda w: _codex(w, sandbox='"workspace-write"'),
        doubt=True,
    ),
    Posture(
        "strict profile, unknown launch flag", user=_codex, argv=("--frobnicate",), doubt=True
    ),
    Posture(
        "strict profile with a broken last line",
        user=lambda w: _codex(w) + 'web_search = "live\n',
        doubt=True,
    ),
)


# --- the labels -------------------------------------------------------------------


def expected(
    agent: str, p: Posture, profile: frozenset[str], mcp: bool, platform: str
) -> tuple[dict[str, str], str]:
    """Each channel's state and the severity, from the labels alone."""
    if platform == "win32":
        p = replace(p, **p.win32)
        if agent == CLAUDE:
            # E12: Claude Code's sandbox does not run on native Windows.
            p = replace(p, blocked=frozenset(), env_hidden=False, net=U)
    by_read: set[str] = set()
    by_shell: set[str] = set()
    for credential in profile:
        if credential in FILES:
            if agent == CLAUDE and p.read and credential not in p.read_denied:
                by_read.add(credential)
            if p.shell and credential not in p.blocked:
                by_shell.add(credential)
        elif credential == ENV:
            if p.shell and not p.env_hidden:
                by_shell.add(credential)
            if agent == CLAUDE and platform == "linux" and p.read and not p.proc_denied:
                by_read.add(credential)  # E3: /proc/self/environ through Read
        elif p.shell:
            by_shell.add(credential)  # an SSH agent signs for any command
    states = {
        FILE_TOOLS: (A if not p.read else U if by_read else C) if agent == CLAUDE else A,
        SHELL_FILES: A if not p.shell else U if by_shell else C,
        SHELL_NETWORK: p.net if p.shell else A,
        WEB: p.web,
        MCP: U if mcp and p.mcp_kept else A,
    }
    if agent == CODEX:
        states[APPS] = p.apps
    readable = by_read | by_shell
    if p.doubt:
        states = {k: A if (agent == CODEX and k == FILE_TOOLS) else K for k in states}
        readable = set(profile)
    external = (p.web == U if p.external is None else p.external) or p.doubt
    external = external or states[MCP] in OPEN or states.get(APPS, A) in OPEN
    egress = any(states.get(k, A) in OPEN for k in curb_reach.EGRESS)
    return states, _severity(bool(readable), external, egress)


def _severity(readable: bool, external: bool, egress: bool) -> str:
    """severity-r1 v1, PRD §9.4, for credentials that are all wide."""
    if readable and egress:
        return "High"  # H1
    if readable:
        return "Medium"  # M1
    return "Low"  # L1


# --- running the cases ------------------------------------------------------------


@dataclass
class Case:
    agent: str
    posture: Posture
    profile: frozenset[str]
    mcp: bool
    platform: str
    expected: dict[str, str]
    severity: str
    report: curb_reach.AgentReport

    @property
    def actual(self) -> dict[str, str]:
        return {c.key: c.state for c in self.report.channels if c.key in self.expected}

    @property
    def agrees(self) -> bool:
        return self.actual == self.expected and self.report.verdict.severity == self.severity

    def describe(self) -> str:
        planted = "+".join(sorted(self.profile)) or "nothing"
        mcp = "MCP" if self.mcp else "no MCP"
        return f"{self.agent}/{self.platform}: {self.posture.name}; {planted}; {mcp}"


def _put(path: Path, content: Content, w: SimpleNamespace) -> None:
    if callable(content):
        content = content(w)
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")


def _workspace(base: Path) -> SimpleNamespace:
    w = SimpleNamespace(
        claude=base / "claude-config",
        codex=base / "codex-home",
        system=base / "system-root",
        plain=base / "work" / "plain",
        dotenv=base / "work" / "dotenv",
        homes={},
    )
    for folder in (w.claude, w.codex, w.plain, w.dotenv):
        folder.mkdir(parents=True)
    (w.dotenv / ".env").write_text("STRIPE_SECRET_KEY=x\n", encoding="utf-8")
    for profile in PROFILES:
        home = base / "homes" / ("-".join(sorted(profile)) or "none")
        home.mkdir(parents=True)
        if AWS in profile:
            _put(home / ".aws" / "credentials", "[default]\naws_access_key_id = x\n", w)
        if SSH in profile:
            _put(home / ".ssh" / "id_ed25519", "-----BEGIN OPENSSH PRIVATE KEY-----\n", w)
        if KUBE in profile:
            _put(home / ".kube" / "config", "contexts:\n- name: prod\n", w)
        w.homes[profile] = home
    return w


def _environment(profile: frozenset[str]) -> dict[str, str]:
    env = {SECRET_ENV: "x"} if ENV in profile else {}
    if AGENT in profile:
        env["SSH_AUTH_SOCK"] = "/tmp/ssh-agent.sock"
    return env


def _place(agent: str, p: Posture, w: SimpleNamespace, mcp: bool, extra: set[str]) -> None:
    if agent == CLAUDE:
        _put(w.claude / "settings.json", p.user, w)
        servers = {"mcpServers": {"pencil": {"command": "pencil"}}} if mcp else None
        _put(w.claude / ".claude.json", servers, w)
        for project in (w.plain, w.dotenv):
            _put(project / ".claude" / "settings.json", p.project, w)
        for platform in OSES:
            folder = curb_settings.managed_dir(platform, w.system)
            _put(folder / "managed-settings.json", p.managed, w)
        home = w.claude
    else:
        user = p.user(w) if callable(p.user) else (p.user or "")
        if mcp:
            user += '\n[mcp_servers.pencil]\ncommand = "pencil"\n'
        _put(w.codex / "config.toml", user or None, w)
        for project in (w.plain, w.dotenv):
            _put(project / ".codex" / "config.toml", p.project, w)
        _put(w.system / "etc" / "codex" / "requirements.toml", p.managed, w)
        home = w.codex
    for name in extra:
        _put(home / name, p.files.get(name), w)


def _cases(agent: str, postures: tuple[Posture, ...], w: SimpleNamespace) -> list[Case]:
    resolve = curb_settings.resolve_claude if agent == CLAUDE else curb_settings.resolve_codex
    extra = {name for p in postures for name in p.files}
    found: dict[tuple[frozenset[str], str], list[curb_credentials.Credential]] = {}
    cases = []
    for p in postures:
        for mcp in (False, True):
            _place(agent, p, w, mcp, extra)
            for platform in OSES:
                for project in (w.plain, w.dotenv):
                    argv = p.argv(w) if callable(p.argv) else p.argv
                    context = parse([agent, *argv], project) if argv else default(agent, project)
                    settings = resolve(context, platform=platform, root=w.system)
                    for profile in PROFILES:
                        if (DOTENV in profile) != (project == w.dotenv):
                            continue
                        key = (profile, platform)
                        if key not in found:
                            found[key] = curb_credentials.find(
                                w.homes[profile], project, _environment(profile), platform
                            )
                        report = curb_reach.assess(
                            context,
                            settings,
                            found[key],
                            platform=platform,
                            home=w.homes[profile],
                            env=_environment(profile),
                            version=BASELINE[agent],
                        )
                        states, severity = expected(agent, p, profile, mcp, platform)
                        cases.append(
                            Case(agent, p, profile, mcp, platform, states, severity, report)
                        )
    return cases


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> list[Case]:
    w = _workspace(tmp_path_factory.mktemp("curb-corpus"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CLAUDE_CONFIG_DIR", str(w.claude))
        mp.setenv("CODEX_HOME", str(w.codex))
        cases = _cases(CLAUDE, CLAUDE_POSTURES, w)
        if sys.version_info >= (3, 11):  # Codex config is TOML
            cases += _cases(CODEX, CODEX_POSTURES, w)
    return cases


def _strata(corpus: list[Case]) -> dict[tuple[str, str, str, str], list[tuple[str, str]]]:
    strata: dict[tuple[str, str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for case in corpus:
        for channel, state in case.expected.items():
            key = (case.agent, case.platform, channel, case.severity)
            strata[key].append((state, case.actual.get(channel, A)))
    return strata


def _agents(corpus: list[Case]) -> list[str]:
    return sorted({case.agent for case in corpus})


# --- the gates --------------------------------------------------------------------


def test_every_stratum_has_enough_cases(corpus):
    strata = _strata(corpus)
    small = [
        f"{key}: {len(strata.get(key, []))}"
        for agent in _agents(corpus)
        for key in (
            (agent, platform, channel, severity)
            for platform in OSES
            for channel in CHANNELS[agent]
            for severity in SEVERITIES
        )
        if len(strata.get(key, [])) < MIN_CASES
    ]
    assert not small, "strata under 20 cases, so not measured:\n" + "\n".join(small)


def test_every_stratum_meets_recall_and_precision(corpus):
    failing = []
    for key, pairs in sorted(_strata(corpus).items()):
        hits = sum(1 for want, got in pairs if want in OPEN and got in OPEN)
        wanted = sum(1 for want, _ in pairs if want in OPEN)
        flagged = sum(1 for _, got in pairs if got in OPEN)
        recall = hits / wanted if wanted else 1.0
        precision = hits / flagged if flagged else 1.0
        if recall < MIN_RATE or precision < MIN_RATE:
            failing.append(f"{key}: recall {recall:.2f}, precision {precision:.2f}")
    assert not failing, "\n".join(failing)


def test_each_agent_agrees_with_its_labels(corpus):
    for agent in _agents(corpus):
        cases = [case for case in corpus if case.agent == agent]
        wrong = [case for case in cases if not case.agrees]
        agreement = 1 - len(wrong) / len(cases)
        detail = "\n".join(
            f"{case.describe()}: want {case.expected} {case.severity}, "
            f"got {case.actual} {case.report.verdict.severity}"
            for case in wrong[:15]
        )
        assert agreement >= MIN_AGREEMENT, f"{agent}: {agreement:.3f} agreement\n{detail}"


def test_invariant_1_wide_readable_and_open_egress_is_always_high(corpus):
    for case in corpus:
        report = case.report
        open_egress = any(c.state in OPEN for c in report.channels if c.key in curb_reach.EGRESS)
        if open_egress and any(r.credential.wide for r in report.readable):
            assert report.verdict.severity == "High", case.describe()


def test_invariant_2_a_read_deny_alone_never_stops_the_shell(corpus):
    for case in corpus:
        if case.agent != CLAUDE or case.actual[SHELL_FILES] == A:
            continue
        for reach in case.report.reach:
            by_rule = any(b.startswith("Read(") for b in reach.blocked_by)
            by_sandbox = any(not b.startswith("Read(") for b in reach.blocked_by)
            if by_rule and not by_sandbox:
                assert SHELL_FILES in reach.via, case.describe()


def test_invariant_3_nothing_unread_is_ever_a_control(corpus):
    for case in corpus:
        if case.posture.doubt:
            assert C not in case.actual.values(), case.describe()
            assert case.report.verdict.evidence == "assumed", case.describe()


def test_invariant_5_an_untested_version_is_never_supported(corpus):
    for case in corpus[::11]:
        report = curb_reach.assess(
            case.report.context,
            case.report.settings,
            [r.credential for r in case.report.reach],
            platform=case.platform,
            home=Path.home(),
            env={},
            version="999.0.0",
        )
        assert not report.supported, case.describe()
        assert report.verdict.evidence == "assumed", case.describe()
        assert all(
            c.evidence == "assumed" and c.disposition == curb_reach.UNSUPPORTED
            for c in report.channels
            if c.key != curb_reach.MODEL
        ), case.describe()


def test_invariant_6_an_mcp_server_never_lowers_severity(corpus):
    rank = {"Low": 0, "Medium": 1, "High": 2}
    without = {
        (c.agent, c.posture.name, c.profile, c.platform): c.report.verdict.severity
        for c in corpus
        if not c.mcp
    }
    for case in corpus:
        if case.mcp:
            key = (case.agent, case.posture.name, case.profile, case.platform)
            assert rank[case.report.verdict.severity] >= rank[without[key]], case.describe()
