"""What one agent launch can reach, channel by channel (Curb PRD §8.2, §9, §10.2).

A channel is one path an agent can use: its file tool, shell commands
reading files, shell commands reaching the network, web fetch and search,
MCP servers, apps. Each has its own controls, so each is judged on its own,
and a report never calls an agent contained unless every channel is.

Version 1 of the severity function does not count approval prompts as
controls (§9.4). So a sandbox only counts when nothing can step outside it
behind a prompt: Claude Code's strict sandbox mode, or Codex with approvals
off. The report says so plainly, because it is the most common reason a
configured sandbox does not lower severity.

Two views come out of one assessment. `redacted` is what any terminal or
JSON caller gets: counts, categories, states, and never a credential's name
or location. `full` is for the desktop window only (§11.1).
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import curb_match
from .curb_context import ASSUMPTION, BASELINE, LaunchContext
from .curb_credentials import Credential
from .curb_settings import ClaudeSettings, CodexSettings
from .curb_severity import ASSUMED, CONFIGURED, Inputs, Verdict, evaluate

CONTROLLED, UNCONTROLLED, ABSENT, UNKNOWN, INFO = (
    "controlled",
    "uncontrolled",
    "absent",
    "unknown",
    "informational",
)
AUTO, GUIDED, EXTERNAL, INFORMATIONAL, UNSUPPORTED = (
    "auto-fixable",
    "guided",
    "external control required",
    "informational",
    "unsupported",
)

FILE_TOOLS = "file_tools"
SHELL_FILES = "shell_files"
SHELL_NETWORK = "shell_network"
WEB = "web"
MCP = "mcp"
APPS = "apps"
MODEL = "model"

LABELS = {
    FILE_TOOLS: "Built-in file tools",
    SHELL_FILES: "Shell commands: files",
    SHELL_NETWORK: "Shell commands: network",
    WEB: "Web fetch and web search",
    MCP: "MCP servers",
    APPS: "Apps and hosted tools",
    MODEL: "Model provider traffic",
}
#: Channels whose openness counts as uncontrolled egress.
EGRESS = (SHELL_NETWORK, WEB, MCP, APPS)


@dataclass(frozen=True)
class Channel:
    key: str
    state: str
    evidence: str
    disposition: str
    why: str
    fix: str | None = None

    @property
    def label(self) -> str:
        return LABELS[self.key]


@dataclass(frozen=True)
class Reach:
    credential: Credential
    #: Channels that can read it.
    via: tuple[str, ...]
    #: What stops it on the channels that cannot, for the full view.
    blocked_by: tuple[str, ...]
    evidence: str


@dataclass
class AgentReport:
    context: LaunchContext
    version: str | None
    supported: bool
    channels: list[Channel]
    reach: list[Reach]
    external_content: list[str]
    verdict: Verdict
    not_checked: list[str]
    assumed: list[str]
    settings: ClaudeSettings | CodexSettings
    mcp_names: list[str] = field(default_factory=list)

    @property
    def readable(self) -> list[Reach]:
        return [r for r in self.reach if r.via]


def assess(
    context: LaunchContext,
    settings: ClaudeSettings | CodexSettings,
    credentials: Sequence[Credential],
    *,
    platform: str,
    home: Path,
    env: Mapping[str, str],
    version: str | None,
) -> AgentReport:
    supported = version == BASELINE[context.agent]
    # Assumptions no single input can settle: they hold the whole verdict.
    blanket = list(settings.assumed)
    if not supported:
        seen = f"version {version}" if version else "an unknown version"
        blanket.append(
            f"{context.label} is {seen}, not the tested {BASELINE[context.agent]}, "
            "so every result is assumed"
        )
    if context.unknown_flags:
        blanket.append(
            "The launch uses flags Curb does not know (" + ", ".join(context.unknown_flags) + "), "
            "so every control counts as unknown"
        )
    if isinstance(settings, ClaudeSettings):
        channels, reach, external = _claude(context, settings, credentials, platform, home, env)
    else:
        channels, reach, external = _codex(context, settings, credentials, platform, home, env)
    if context.source.startswith("scheduled"):
        external.append("runs unattended on a schedule")

    if context.unknown_flags or settings.assumed:
        # An unknown flag or an unreadable setting could turn a channel on as
        # easily as off (§9.4, invariant 3). Codex has no file tool to add.
        why = (
            "an unknown launch flag" if context.unknown_flags else "a setting Curb could not read"
        )
        fixed = {MODEL} | ({FILE_TOOLS} if isinstance(settings, CodexSettings) else set())
        channels = [
            c if c.key in fixed else Channel(c.key, UNKNOWN, ASSUMED, c.disposition, why, c.fix)
            for c in channels
        ]
        reach = [Reach(r.credential, r.via or (SHELL_FILES,), (), ASSUMED) for r in reach]
    if not supported:
        channels = [
            Channel(
                c.key,
                c.state,
                ASSUMED,
                UNSUPPORTED if c.key != MODEL else c.disposition,
                c.why,
                c.fix,
            )
            for c in channels
        ]
    elif blanket:
        channels = [
            Channel(c.key, c.state, ASSUMED, c.disposition, c.why, c.fix) for c in channels
        ]

    readable = [r for r in reach if r.via]
    local = list(settings.defaults) + [a for r in readable for a in _reach_assumptions(r)]
    worst = evaluate(
        Inputs(
            wide_readable=any(r.credential.wide for r in readable),
            any_readable=bool(readable),
            external_content=bool(external),
            egress_uncontrolled=_egress(channels, assumed_too=True),
            assumptions=tuple(blanket + local),
        )
    )
    verdict = worst
    if not blanket and local:
        # An assumption only matters if it sets the severity (§9.4): score
        # again with every assumed input given its better value instead.
        settled = [r for r in readable if r.evidence == CONFIGURED]
        best = evaluate(
            Inputs(
                wide_readable=any(r.credential.wide for r in settled),
                any_readable=bool(settled),
                external_content=bool(external),
                egress_uncontrolled=_egress(channels, assumed_too=False),
            )
        )
        if best.rule == worst.rule:
            verdict = best
    return AgentReport(
        context=context,
        version=version,
        supported=supported,
        channels=channels,
        reach=reach,
        external_content=external,
        verdict=verdict,
        not_checked=list(settings.not_checked),
        assumed=blanket + local,
        settings=settings,
        mcp_names=[server.name for server in settings.mcp],
    )


def _egress(channels: Sequence[Channel], *, assumed_too: bool) -> bool:
    return any(
        c.state in (UNCONTROLLED, UNKNOWN) and (assumed_too or c.evidence == CONFIGURED)
        for c in channels
        if c.key in EGRESS
    )


def _reach_assumptions(reach: Reach) -> list[str]:
    # An unknown scope counting as wide is the rule, not an assumption (§9.4).
    if reach.evidence == ASSUMED and reach.credential.via_shell:
        return [f"{reach.credential.label} is assumed readable through a shell command"]
    return []


# --- Claude Code --------------------------------------------------------------


def _bare_denied(settings: ClaudeSettings, tool: str) -> bool:
    return any(
        rule.tool == tool and (rule.spec is None or rule.spec.strip() in ("*", ""))
        for rule in settings.deny
    )


def _tool_available(context: LaunchContext, settings: ClaudeSettings, tool: str) -> bool:
    if _bare_denied(settings, tool):
        return False
    if context.tools is not None and context.tools != ("default",):
        return tool in context.tools
    if context.restricted and tool in ("Bash", "PowerShell", "Monitor", "WebFetch"):
        return False
    return True


def _claude(
    context: LaunchContext,
    settings: ClaudeSettings,
    credentials: Sequence[Credential],
    platform: str,
    home: Path,
    env: Mapping[str, str],
) -> tuple[list[Channel], list[Reach], list[str]]:
    cwd = context.cwd
    shell_tools = ["Bash", "Monitor"] + (["PowerShell"] if platform == "win32" else [])
    shell = any(_tool_available(context, settings, tool) for tool in shell_tools)
    read_tool = _tool_available(context, settings, "Read")
    sandbox_on = settings.sandbox_enabled and platform != "win32"
    prompts_off = settings.mode == "dontAsk" or context.prompts_denied
    escape_open = settings.allow_unsandboxed and not prompts_off
    boundary = sandbox_on and not escape_open and not settings.excluded_commands
    files_isolated = boundary and not settings.filesystem_disabled
    deny_rules = [(r.tool, r.spec, r.source, r.anchor) for r in settings.deny]

    reach: list[Reach] = []
    for credential in credentials:
        via: list[str] = []
        blocked: list[str] = []
        evidence = CONFIGURED
        if credential.via_shell:
            if credential.kind == "env":
                # credentials.envVars only reaches sandboxed commands (E12).
                masked = set(settings.credential_env) if boundary else set()
                if shell and set(credential.names) - masked:
                    via.append(SHELL_FILES)
                elif shell:
                    blocked.append("sandbox credentials.envVars")
                if platform == "linux" and read_tool:
                    proc = Path("/proc/self/environ")
                    rule = curb_match.claude_read_denied(proc, deny_rules, cwd=cwd, home=home)
                    if rule is None:
                        via.append(FILE_TOOLS)  # E3: /proc/self/environ through Read
                    else:
                        blocked.append(rule)
            elif shell:
                via.append(SHELL_FILES)
                evidence = ASSUMED
        else:
            if read_tool:
                hits = [
                    curb_match.claude_read_denied(path, deny_rules, cwd=cwd, home=home)
                    for path in credential.paths
                ]
                outside = context.restricted and not all(
                    curb_match.within(path, settings.working_dirs) for path in credential.paths
                )
                if all(hits) or outside:
                    blocked.extend(h for h in hits if h)
                else:
                    via.append(FILE_TOOLS)
            if shell:
                stops = [
                    _claude_shell_block(path, settings, files_isolated, home)
                    for path in credential.paths
                ]
                if credential.paths and all(stops):
                    blocked.extend(s for s in stops if s)
                else:
                    via.append(SHELL_FILES)
        reach.append(Reach(credential, tuple(dict.fromkeys(via)), tuple(blocked), evidence))

    channels: list[Channel] = []
    readable_by = Counter(channel for r in reach for channel in r.via)

    if not read_tool:
        channels.append(
            Channel(
                FILE_TOOLS,
                ABSENT,
                CONFIGURED,
                INFORMATIONAL,
                "the Read tool is not available to this launch",
            )
        )
    elif readable_by[FILE_TOOLS]:
        channels.append(
            Channel(
                FILE_TOOLS,
                UNCONTROLLED,
                CONFIGURED,
                AUTO,
                f"{readable_by[FILE_TOOLS]} credential source(s) have no Read deny rule",
                "Add Read deny rules for the credential files (shown in the window)",
            )
        )
    else:
        channels.append(
            Channel(
                FILE_TOOLS,
                CONTROLLED,
                CONFIGURED,
                AUTO,
                "every credential found has a Read deny rule",
            )
        )

    if not shell:
        channels.append(
            Channel(
                SHELL_FILES,
                ABSENT,
                CONFIGURED,
                INFORMATIONAL,
                "shell tools are not available to this launch",
            )
        )
        channels.append(
            Channel(
                SHELL_NETWORK,
                ABSENT,
                CONFIGURED,
                INFORMATIONAL,
                "shell tools are not available to this launch",
            )
        )
    else:
        why_open = _claude_open_reason(settings, sandbox_on, escape_open, platform)
        if readable_by[SHELL_FILES]:
            channels.append(
                Channel(
                    SHELL_FILES,
                    UNCONTROLLED,
                    CONFIGURED,
                    AUTO,
                    f"{readable_by[SHELL_FILES]} credential source(s) are readable: {why_open}",
                    "Turn on the sandbox with denyRead or credentials entries for them, "
                    "and set sandbox.allowUnsandboxedCommands to false",
                )
            )
        else:
            channels.append(
                Channel(
                    SHELL_FILES,
                    CONTROLLED,
                    CONFIGURED,
                    AUTO,
                    "no credential found is readable by shell commands",
                )
            )
        restricted = settings.strict_allowlist or settings.managed_domains_only or prompts_off
        wildcard = any(d.strip() in ("*", "*:*") for d in settings.allowed_domains)
        if settings.mode == "bypassPermissions" and not (
            settings.strict_allowlist or settings.managed_domains_only
        ):
            restricted = False
        if boundary and restricted and not wildcard:
            channels.append(
                Channel(
                    SHELL_NETWORK,
                    CONTROLLED,
                    CONFIGURED,
                    AUTO,
                    "the sandbox allows only its listed domains",
                )
            )
        else:
            reason = (
                why_open
                if not boundary
                else (
                    "the allowlist admits every host"
                    if wildcard
                    else "a host outside the allowlist is allowed after a prompt"
                )
            )
            channels.append(
                Channel(
                    SHELL_NETWORK,
                    UNCONTROLLED,
                    CONFIGURED,
                    AUTO,
                    reason,
                    "Turn on the sandbox, set sandbox.network.strictAllowlist to true, "
                    "and set sandbox.allowUnsandboxedCommands to false",
                )
            )

    fetch = _tool_available(context, settings, "WebFetch") and not any(
        r.tool == "WebFetch" and (r.spec or "").strip() == "domain:*" for r in settings.deny
    )
    search = _tool_available(context, settings, "WebSearch")
    external: list[str] = []
    if fetch or search:
        on = " and ".join(
            name for name, live in (("WebFetch", fetch), ("WebSearch", search)) if live
        )
        channels.append(
            Channel(
                WEB,
                UNCONTROLLED,
                CONFIGURED,
                GUIDED,
                f"{on} can reach any site",
                "Deny WebFetch and WebSearch, or deny WebFetch(domain:*) and allow only "
                "the domains you need",
            )
        )
        external.append(f"{on} {'are' if fetch and search else 'is'} available")
    else:
        channels.append(
            Channel(WEB, CONTROLLED, CONFIGURED, GUIDED, "WebFetch and WebSearch are denied")
        )

    channels.append(_mcp_channel(settings.mcp, "Claude Code"))
    if settings.mcp:
        external.append(f"{len(settings.mcp)} MCP server(s) are configured")
    channels.append(_model_channel())
    return channels, reach, external


def _claude_shell_block(
    path: Path, settings: ClaudeSettings, isolated: bool, home: Path
) -> str | None:
    if not isolated:
        return None
    entry = curb_match.sandbox_read_denied(
        path,
        deny=[*settings.deny_read, *settings.credential_files],
        allow=settings.allow_read,
        home=home,
    )
    if entry:
        return f"sandbox denyRead {entry}"
    if settings.block_reads_outside and not curb_match.within(path, settings.working_dirs):
        return "permissions.blockReadsOutsideWorkingDirectories"
    return None


def _claude_open_reason(
    settings: ClaudeSettings, sandbox_on: bool, escape_open: bool, platform: str
) -> str:
    if settings.sandbox_enabled and platform == "win32":
        return "the sandbox does not run on native Windows"
    if not sandbox_on:
        return "the sandbox is off"
    if escape_open:
        return "commands can retry outside the sandbox after a prompt (allowUnsandboxedCommands)"
    if settings.excluded_commands:
        return "excludedCommands run outside the sandbox"
    if settings.filesystem_disabled:
        return "sandbox filesystem isolation is disabled"
    return "the sandbox does not deny these paths"


# --- Codex --------------------------------------------------------------------


def _codex(
    context: LaunchContext,
    settings: CodexSettings,
    credentials: Sequence[Credential],
    platform: str,
    home: Path,
    env: Mapping[str, str],
) -> tuple[list[Channel], list[Reach], list[str]]:
    sandboxed = settings.sandbox in ("read-only", "workspace-write", "profile")
    boundary = sandboxed and settings.approval == "never" and not settings.reviewer_auto
    deny = [(entry, context.cwd) for entry in settings.deny_read]
    sandbox_evidence = (
        ASSUMED if (settings.sandbox_assumed or settings.approval_assumed) else (CONFIGURED)
    )

    reach: list[Reach] = []
    for credential in credentials:
        via: list[str] = []
        blocked: list[str] = []
        evidence = CONFIGURED
        if credential.kind == "env":
            kept = [name for name in credential.names if _codex_env_kept(name, settings)]
            if kept:
                via.append(SHELL_FILES)
            else:
                blocked.append("shell_environment_policy")
        elif credential.via_shell:
            via.append(SHELL_FILES)
            evidence = ASSUMED
        else:
            stops = [
                _codex_read_stop(path, settings, deny, home) if boundary else None
                for path in credential.paths
            ]
            if credential.paths and all(stops):
                blocked.extend(s for s in stops if s)
            else:
                via.append(SHELL_FILES)
        reach.append(Reach(credential, tuple(via), tuple(blocked), evidence))

    readable = sum(1 for r in reach if r.via)
    if settings.sandbox == "danger-full-access":
        why = "commands run with no sandbox"
    elif settings.sandbox == "profile" and boundary:
        why = "the permissions profile grants these paths"
    elif not boundary:
        why = "commands can ask to run outside the sandbox (approval_policy is not never)"
    else:
        why = "the sandbox does not deny these paths"
    channels = [
        Channel(
            FILE_TOOLS,
            ABSENT,
            CONFIGURED,
            INFORMATIONAL,
            "Codex reads files through shell commands",
        ),
        Channel(
            SHELL_FILES,
            UNCONTROLLED if readable else CONTROLLED,
            sandbox_evidence,
            AUTO,
            f"{readable} credential source(s) are readable: {why}"
            if readable
            else "no credential found is readable by shell commands",
            "Set approval_policy to never and add deny_read entries for them"
            if readable
            else None,
        ),
    ]

    allow_all = settings.network_domains.get("*") == "allow"
    if settings.sandbox == "danger-full-access":
        net_state, net_why = UNCONTROLLED, "commands run with no sandbox"
    elif not boundary:
        net_state = UNCONTROLLED
        net_why = "commands can ask to run outside the sandbox (approval_policy is not never)"
    elif settings.sandbox == "read-only" or not settings.network_access:  # a profile too
        net_state, net_why = CONTROLLED, "sandboxed commands have no network"
    elif (settings.network_domains or settings.proxy_enabled) and not allow_all:
        net_state, net_why = CONTROLLED, "sandboxed commands reach only the allowed domains"
    else:
        net_state, net_why = UNCONTROLLED, "sandboxed commands have network access"
    channels.append(
        Channel(
            SHELL_NETWORK,
            net_state,
            sandbox_evidence,
            AUTO,
            net_why,
            None
            if net_state == CONTROLLED
            else (
                "Set approval_policy to never, and keep network_access off or limit it to "
                "allowed domains"
            ),
        )
    )

    external: list[str] = []
    web_evidence = ASSUMED if settings.web_search_assumed else CONFIGURED
    if settings.web_search == "disabled":
        channels.append(Channel(WEB, CONTROLLED, web_evidence, GUIDED, "web search is disabled"))
    elif settings.web_search == "cached":
        channels.append(
            Channel(
                WEB,
                CONTROLLED,
                web_evidence,
                GUIDED,
                "web search answers from a cached index, with no live access",
            )
        )
        external.append("web search results come from a cached index")
    else:
        channels.append(
            Channel(
                WEB,
                UNCONTROLLED,
                web_evidence,
                GUIDED,
                f"web search is {settings.web_search}, with live access",
                "Set web_search to cached or disabled",
            )
        )
        external.append(f"web search is {settings.web_search}")

    channels.append(_mcp_channel(settings.mcp, "Codex"))
    if settings.mcp:
        external.append(f"{len(settings.mcp)} MCP server(s) are configured")
    if settings.apps is False:
        channels.append(Channel(APPS, ABSENT, CONFIGURED, INFORMATIONAL, "apps are turned off"))
    else:
        channels.append(
            Channel(
                APPS,
                UNCONTROLLED if settings.apps else UNKNOWN,
                CONFIGURED if settings.apps else ASSUMED,
                EXTERNAL,
                "apps are enabled, and Codex's domain rules do not cover them"
                if settings.apps
                else "apps are on by default, and the ones connected to your ChatGPT account "
                "can't be read here",
                "Set features.apps to false if you do not use apps",
            )
        )
        external.append("apps are enabled" if settings.apps else "apps may be connected")
    channels.append(_model_channel())
    return channels, reach, external


def _codex_read_stop(
    path: Path, settings: CodexSettings, deny: list[tuple[str, Path]], home: Path
) -> str | None:
    """What keeps sandboxed commands from reading a file, or None."""
    admin = curb_match.sandbox_read_denied(path, deny=deny, allow=[], home=home)
    if admin:
        return f"deny_read {admin}"
    if settings.sandbox != "profile":
        return None  # the older sandbox modes read everywhere
    return curb_match.codex_profile_reads(
        path,
        settings.profile_entries,
        roots=settings.workspace_roots,
        home=home,
        base=settings.profile_base,
    )


def _codex_env_kept(name: str, settings: CodexSettings) -> bool:
    """Whether Codex hands an environment variable to the commands it runs."""
    if settings.env_inherit in ("none", "core"):
        return False
    upper = name.upper()
    if not settings.env_keep_secret_names and any(w in upper for w in ("KEY", "SECRET", "TOKEN")):
        return False
    for pattern, verdict in settings.env_filters.items():
        if verdict == "exclude" and fnmatch.fnmatchcase(upper, pattern.upper()):
            return False
    if settings.env_include_only:
        return any(fnmatch.fnmatchcase(upper, p.upper()) for p in settings.env_include_only)
    return True


# --- shared -------------------------------------------------------------------


def _mcp_channel(servers: Sequence[Any], agent: str) -> Channel:
    if not servers:
        return Channel(MCP, ABSENT, CONFIGURED, INFORMATIONAL, "no MCP servers are configured")
    return Channel(
        MCP,
        UNCONTROLLED,
        CONFIGURED,
        EXTERNAL,
        f"{len(servers)} MCP server(s); their own network access is outside {agent}'s control",
        "Remove servers you do not use",
    )


def _model_channel() -> Channel:
    return Channel(
        MODEL,
        INFO,
        CONFIGURED,
        INFORMATIONAL,
        "prompts and tool results go to the model provider; not counted",
    )


# --- views --------------------------------------------------------------------


def redacted(report: AgentReport) -> dict[str, Any]:
    """What a terminal or JSON caller may see: no credential names or locations."""
    readable = report.readable
    by_category = Counter(r.credential.category for r in readable)
    return {
        "agent": report.context.agent,
        "label": report.context.label,
        "launch": report.context.describe(),
        "directory": str(report.context.cwd),
        "assumption": ASSUMPTION,
        "version": report.version,
        "supported": report.supported,
        "baseline": BASELINE[report.context.agent],
        "severity": {
            "level": report.verdict.severity,
            "rule": report.verdict.rule,
            "reason": report.verdict.text,
            "evidence": report.verdict.evidence,
            "function": report.verdict.function,
        },
        "channels": [
            {
                "channel": c.label,
                "state": c.state,
                "evidence": c.evidence,
                "disposition": c.disposition,
                "why": c.why,
                "fix": c.fix,
            }
            for c in report.channels
        ],
        "credentials": {
            "found": len(report.reach),
            "readable": len(readable),
            "wide": sum(1 for r in readable if r.credential.wide),
            "by_category": dict(sorted(by_category.items())),
        },
        "external_content": report.external_content,
        "not_checked": report.not_checked,
        "assumed": report.assumed,
    }


def full(report: AgentReport) -> dict[str, Any]:
    """Everything, for the desktop window only. Never printed or written to disk."""
    out = redacted(report)
    out["launch_command"] = list(report.context.command)
    out["credentials"]["items"] = [
        {
            "label": r.credential.label,
            "category": r.credential.category,
            "paths": [str(p) for p in r.credential.paths],
            "names": list(r.credential.names),
            "identity": r.credential.identity,
            "expires": r.credential.expires,
            "wide": r.credential.wide,
            "readable_through": [LABELS[c] for c in r.via],
            "blocked_by": list(r.blocked_by),
            "evidence": r.evidence,
        }
        for r in report.reach
    ]
    out["settings_layers"] = [
        {
            "name": layer.name,
            "where": layer.where,
            "controlled_by": layer.controller,
            "present": layer.present,
            "problem": layer.error,
        }
        for layer in report.settings.layers
    ]
    out["mcp_servers"] = [
        {
            "name": s.name,
            "transport": s.transport,
            "command": list(s.command[:1]),
            "url_host": (s.url or "").split("://", 1)[-1].split("/", 1)[0] or None,
            "env_names": list(s.env_names),
            "configured_in": s.source,
            "controlled_by": s.controller,
        }
        for s in report.settings.mcp
    ]
    return out
