"""The tighten-only test: would a settings change leave every channel no broader? (Curb PRD §10.8)

A change is tighten-only only if all of these hold:

- every key it touches is one Curb understands, and moves the tighter way:
  a deny list only grows, an allow list only shrinks, a sandbox only turns
  on. A key Curb cannot judge, such as `env`, a helper command, a proxy or
  an endpoint, makes the change unproven, and unproven is not tighten-only;
- worked out with the same resolver and reach rules `curb map` uses, no
  channel's state moves toward open, and no probe file becomes readable
  through a channel that could not read it before;
- for Claude Code, no MCP server would load that could not before, probed
  with servers that reuse every listed name, command and URL, and with
  strangers. That probe is what catches the known traps (E14): removing
  the last `serverCommand` entry lets any program run under an allowed
  name, removing `allowedMcpServers` allows every server, and dropping
  `allowManagedMcpServersOnly` lets other scopes' allowlists count.

An edit kind that usually tightens is a candidate, never proof.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import curb_reach, curb_settings
from .curb_context import CLAUDE, LaunchContext
from .curb_credentials import Credential
from .curb_settings import ClaudeSettings, McpServer

_RANK = {
    curb_reach.ABSENT: 0,
    curb_reach.CONTROLLED: 1,
    curb_reach.INFO: 1,
    curb_reach.UNKNOWN: 2,
    curb_reach.UNCONTROLLED: 3,
}


@dataclass(frozen=True)
class Verdict:
    broader: tuple[str, ...]
    unproven: tuple[str, ...]

    @property
    def tighten_only(self) -> bool:
        return not self.broader and not self.unproven


# --- reading the file both ways ---------------------------------------------------------


def load(path: Path) -> dict[str, Any]:
    """A settings file's data, {} when absent. Raises ValueError when unreadable."""
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".toml":
        loads = curb_settings._toml_loads()
        if loads is None:
            raise ValueError("Python 3.10 has no TOML reader")
        return loads(text)
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return data


def _flat(data: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    # Tuples, not dotted strings: a path key such as "~/.aws" holds a dot.
    out: dict[tuple[str, ...], Any] = {}
    for key, value in data.items():
        parts = (*prefix, str(key))
        if isinstance(value, Mapping):
            out.update(_flat(value, parts))  # an empty table adds nothing
        else:
            out[parts] = value
    return out


# --- key rules ------------------------------------------------------------------------

Rule = Callable[[Any, Any], bool]  # (before, after) -> True when no broader


def _grows(before: Any, after: Any) -> bool:
    return _items(before) <= _items(after)


def _shrinks(before: Any, after: Any) -> bool:
    return _items(after) <= _items(before)


def _items(value: Any) -> set[str]:
    if value is None:
        return set()
    seq = value if isinstance(value, list) else [value]
    return {json.dumps(item, sort_keys=True) for item in seq}


def _flag(tight: Any, default: Any) -> Rule:
    """A setting whose `tight` value is the tighter one; `default` when unset."""

    def rule(before: Any, after: Any) -> bool:
        was = default if before is None else before
        now = default if after is None else after
        return bool(now == tight or was == now or was != tight)

    return rule


def _ranked(order: Mapping[str, int], default: str) -> Rule:
    def rank(value: Any) -> int:
        return order.get(str(value if value is not None else default), max(order.values()) + 1)

    return lambda before, after: rank(after) <= rank(before)


def _approval_rank(value: Any) -> int:
    if isinstance(value, Mapping):
        granular = value.get("granular")
        return (
            0 if isinstance(granular, Mapping) and granular.get("sandbox_approval") is False else 1
        )
    return 0 if value == "never" else 1


def _include_only(before: Any, after: Any) -> bool:
    return before is None and after is not None or _shrinks(before, after)


def _deny_value(before: Any, after: Any) -> bool:
    return bool(after == "deny" or (before != "deny" and before == after))


def _domain_verdict(before: Any, after: Any) -> bool:
    order = {"deny": 0, None: 1, "allow": 2}
    return order.get(after, 3) <= order.get(before, 3)


CLAUDE_RULES: dict[str, Rule] = {
    "permissions.deny": _grows,
    "permissions.ask": _grows,
    "permissions.allow": _shrinks,
    "permissions.additionalDirectories": _shrinks,
    "permissions.defaultMode": _ranked(
        {
            "plan": 0,
            "dontAsk": 0,
            "default": 1,
            "manual": 1,
            "acceptEdits": 2,
            "auto": 3,
            "bypassPermissions": 4,
        },
        "default",
    ),
    "permissions.disableBypassPermissionsMode": _flag("disable", None),
    "permissions.disableAutoMode": _flag("disable", None),
    "permissions.blockReadsOutsideWorkingDirectories": _flag(True, False),
    "sandbox.enabled": _flag(True, False),
    "sandbox.allowUnsandboxedCommands": _flag(False, True),
    "sandbox.excludedCommands": _shrinks,
    "sandbox.filesystem.denyRead": _grows,
    "sandbox.filesystem.allowRead": _shrinks,
    "sandbox.network.allowedDomains": _shrinks,
    "sandbox.network.strictAllowlist": _flag(True, False),
    "sandbox.network.allowManagedDomainsOnly": _flag(True, False),
    "sandbox.credentials.files": _grows,
    "sandbox.credentials.envVars": _grows,
    "deniedMcpServers": _grows,
    # Judged by the server probe, which knows the type rules.
    "allowedMcpServers": lambda before, after: True,
    "allowManagedMcpServersOnly": _flag(True, False),
    "enabledMcpjsonServers": _shrinks,
    "disabledMcpjsonServers": _grows,
    "enableAllProjectMcpServers": _flag(False, False),
}

CODEX_RULES: dict[str, Rule] = {
    "sandbox_mode": _ranked(
        {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}, "workspace-write"
    ),
    "approval_policy": lambda before, after: _approval_rank(after) <= _approval_rank(before),
    "approvals_reviewer": _ranked({"user": 0, "auto_review": 1}, "user"),
    "sandbox_workspace_write.network_access": _flag(False, False),
    "web_search": _ranked({"disabled": 0, "cached": 1, "live": 2}, "cached"),
    "tools.web_search": _flag(False, None),
    "features.apps": _flag(False, True),
    "apps.*.enabled": _flag(False, True),
    "shell_environment_policy.inherit": _ranked({"none": 0, "core": 1, "all": 2}, "all"),
    "shell_environment_policy.exclude": _grows,
    "shell_environment_policy.include_only": _include_only,
    "shell_environment_policy.ignore_default_excludes": _flag(False, True),
    "permissions.*.filesystem.*": _deny_value,
    "permissions.*.network.enabled": _flag(False, True),
    "permissions.*.network.domains.*": _domain_verdict,
    "mcp_servers.*.enabled": _flag(False, True),
}


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def _rule(rules: Mapping[str, Rule], key: tuple[str, ...]) -> Rule | None:
    for pattern, rule in rules.items():
        wanted = pattern.split(".")
        if len(wanted) == len(key) and all(
            w in ("*", k) for w, k in zip(wanted, key, strict=True)
        ):
            return rule
    return None


def _key_findings(
    agent: str, before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    rules = CLAUDE_RULES if agent == CLAUDE else CODEX_RULES
    flat_before, flat_after = _flat(before), _flat(after)
    broader: list[str] = []
    unproven: list[str] = []
    gone = set(_table(before, "mcp_servers")) - set(_table(after, "mcp_servers"))
    for key in sorted(set(flat_before) | set(flat_after)):
        was, now = flat_before.get(key), flat_after.get(key)
        if was == now:
            continue
        if agent != CLAUDE and key[0] == "mcp_servers" and len(key) > 1 and key[1] in gone:
            continue  # a server taken away entirely only ever tightens
        rule, name = _rule(rules, key), ".".join(key)
        if rule is None:
            unproven.append(f"{name} changes, and Curb cannot establish its effect")
        elif not rule(was, now):
            broader.append(f"{name} moves the looser way")
    return broader, unproven


# --- probes -------------------------------------------------------------------------------


def _probe_servers(*settings: ClaudeSettings) -> list[McpServer]:
    names, commands, urls = {"curb-probe-stranger"}, set(), {"https://curb-probe.invalid/mcp"}
    for one in settings:
        for entry in [*(one.mcp_allowlist or []), *one.mcp_denylist]:
            if "serverName" in entry:
                names.add(str(entry["serverName"]))
            if "serverCommand" in entry and isinstance(entry["serverCommand"], list):
                commands.add(json.dumps([str(part) for part in entry["serverCommand"]]))
            if "serverUrl" in entry:
                urls.add(str(entry["serverUrl"]).replace("*", "x"))
    probes = []
    for name in sorted(names):
        probes.append(
            McpServer(name, "stdio", ("curb-probe-any-program",), None, (), "probe", "user")
        )
        probes.append(
            McpServer(name, "http", (), "https://curb-probe.invalid/x", (), "probe", "user")
        )
    for command in sorted(commands):
        argv = tuple(json.loads(command))
        probes.append(McpServer("curb-probe-renamed", "stdio", argv, None, (), "probe", "user"))
    for url in sorted(urls):
        probes.append(McpServer("curb-probe-renamed", "http", (), url, (), "probe", "user"))
    return probes


def _admitted(settings: ClaudeSettings, server: McpServer) -> bool:
    return curb_settings.admits(server, settings.mcp_allowlist, settings.mcp_denylist)


def check(
    context: LaunchContext,
    path: Path,
    after: Mapping[str, Any],
    *,
    probes: Sequence[Credential],
    platform: str,
    home: Path,
    env: Mapping[str, str],
    root: Path | None = None,
) -> Verdict:
    """Judge one file's change for one launch context. Writes nothing."""
    try:
        before = load(path)
    except ValueError:
        return Verdict((), (f"{path.name} cannot be read now, so no change can be judged",))
    broader, unproven = _key_findings(context.agent, before, after)
    was = curb_settings.resolve(context, platform=platform, root=root)
    with curb_settings.replaced({path: after}):
        now = curb_settings.resolve(context, platform=platform, root=root)

    def assess(settings: Any) -> curb_reach.AgentReport:
        return curb_reach.assess(
            context, settings, list(probes), platform=platform, home=home, env=env, version=None
        )

    old, new = assess(was), assess(now)
    old_state = {c.key: c.state for c in old.channels}
    for channel in new.channels:
        if _RANK[channel.state] > _RANK[old_state.get(channel.key, curb_reach.ABSENT)]:
            broader.append(f"the {channel.label.lower()} channel would be broader")
    for before_reach, after_reach in zip(old.reach, new.reach, strict=True):
        gained = set(after_reach.via) - set(before_reach.via)
        if gained:
            channels = ", ".join(sorted(curb_reach.LABELS[c].lower() for c in gained))
            broader.append(f"a probe file would become readable through {channels}")
    if isinstance(was, ClaudeSettings) and isinstance(now, ClaudeSettings):
        for server in _probe_servers(was, now):
            if _admitted(now, server) and not _admitted(was, server):
                broader.append("an MCP server could load that could not before")
                break
    return Verdict(tuple(dict.fromkeys(broader)), tuple(dict.fromkeys(unproven)))
