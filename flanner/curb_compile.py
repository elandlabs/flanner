"""One org policy, compiled into each agent's own settings (Curb PRD §10.8).

A policy's rules are written once, in Curb's channels:

    deny_read  paths no agent may read by any channel, such as "~/.aws";
               each means the file or folder and everything under it
    sandbox    "required": the agent's sandbox, with no way around it
    network    {"allowed_domains": [...]}: all sandboxed commands may reach;
               it needs the sandbox, so it turns the sandbox on too
    web        "off": no web fetch and no web search
    mcp        {"allowed": [{"name", "command": [...]} or {"name", "url"}]}:
               the only MCP servers that may load; {"name"} alone matches
               by name, which Claude Code honours less than it seems (E14)

The rules compile into Claude Code's managed settings, Codex's
`requirements.toml` and an NVIDIA OpenShell policy, for an administrator
to deliver as admin-owned settings, and into each agent's user settings,
which is what a device without admin rights can write. Whatever a target
cannot express comes back as a note or a guided step, never dropped.
Compiling reads and writes nothing.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import yaml

from .curb_fix import _append, _set, _toml_edit, _toml_key, _toml_parse
from .curb_match import posix

RULES = ("deny_read", "sandbox", "network", "web", "mcp")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
#: What OpenShell lets sandboxed programs read, from its own example policy.
OPENSHELL_READ_ONLY = ("/usr", "/lib", "/etc")
OPENSHELL_READ_WRITE = ("/tmp",)  # noqa: S108 - a path inside the sandbox, never opened here


@dataclass(frozen=True)
class Server:
    name: str
    command: tuple[str, ...] = ()
    url: str = ""


@dataclass(frozen=True)
class Rules:
    deny_read: tuple[str, ...] = ()
    sandbox: bool = False
    allowed_domains: tuple[str, ...] | None = None
    web_off: bool = False
    mcp: tuple[Server, ...] | None = None
    #: Rule names this flanner does not know, from a newer policy schema.
    unknown: tuple[str, ...] = ()

    @property
    def needs_sandbox(self) -> bool:
        return self.sandbox or self.allowed_domains is not None


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{what} must be a list of strings")
    return tuple(value)


def _path(text: str) -> str:
    if any(c in text for c in "*?["):
        raise ValueError(f"deny_read takes paths, not patterns: {text}")
    if not (text.startswith(("~/", "/")) or _DRIVE.match(text)):
        raise ValueError(f"deny_read paths start with ~/ or are absolute: {text}")
    if ".." in re.split(r"[\\/]", text):
        raise ValueError(f"deny_read paths may not climb with ..: {text}")
    return text.rstrip("/\\") or text


def _server(entry: Any) -> Server:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
        raise ValueError("each allowed MCP server needs a name")
    command, url = entry.get("command"), entry.get("url")
    if command is not None and url is not None:
        raise ValueError(f"MCP server {entry['name']} takes a command or a url, not both")
    if command is None and url is None:
        return Server(entry["name"])  # by name alone: the weakest match
    if url is not None:
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ValueError(f"MCP server {entry['name']} has no usable url")
        return Server(entry["name"], url=url)
    argv = (command,) if isinstance(command, str) else _strings(command, "command")
    return Server(entry["name"], command=tuple(argv))


def parse(raw: Mapping[str, Any]) -> Rules:
    """A policy's rules. Raises ValueError for a rule this schema cannot read."""
    deny = tuple(_path(p) for p in _strings(raw.get("deny_read", []), "deny_read"))
    sandbox = raw.get("sandbox")
    if sandbox not in (None, "required"):
        raise ValueError('sandbox can only be "required"')
    network = raw.get("network")
    domains: tuple[str, ...] | None = None
    if network is not None:
        if not isinstance(network, Mapping):
            raise ValueError("network must be a table")
        domains = _strings(network.get("allowed_domains", []), "allowed_domains") or ()
    web = raw.get("web")
    if web not in (None, "off"):
        raise ValueError('web can only be "off"')
    mcp = raw.get("mcp")
    servers: tuple[Server, ...] | None = None
    if mcp is not None:
        allowed = mcp.get("allowed") if isinstance(mcp, Mapping) else None
        if not isinstance(allowed, list):
            raise ValueError("mcp needs an allowed list, which may be empty")
        servers = tuple(_server(e) for e in allowed)
    return Rules(
        deny_read=deny,
        sandbox=sandbox == "required",
        allowed_domains=domains,
        web_off=web == "off",
        mcp=servers,
        unknown=tuple(sorted(k for k in raw if k not in RULES)),
    )


# --- Claude Code ---------------------------------------------------------------------------


def _claude_reads(path: str) -> list[str]:
    """Read rules for a path and everything under it, in Claude Code's rule syntax."""
    if path.startswith("~/"):
        base = path
    elif _DRIVE.match(path):
        base = "/" + posix(path)
    else:
        base = "/" + path  # `//abs`: a single slash would be relative to the settings file
    return [f"Read({base})", f"Read({base}/**)"]


def _claude_server(server: Server) -> dict[str, Any]:
    if server.url:
        return {"serverUrl": server.url}
    if server.command:
        return {"serverCommand": list(server.command)}
    return {"serverName": server.name}  # matches less than it seems (E14)


def claude_managed(rules: Rules) -> tuple[dict[str, Any], list[str]]:
    """Claude Code's `managed-settings.json` for the policy, and what it cannot do."""
    data: dict[str, Any] = {}
    deny = [r for p in rules.deny_read for r in _claude_reads(p)]
    deny += ["WebFetch", "WebSearch"] if rules.web_off else []
    if deny:
        data["permissions"] = {"deny": deny}
    sandbox: dict[str, Any] = {}
    if rules.needs_sandbox:
        sandbox.update(enabled=True, allowUnsandboxedCommands=False)
    if rules.deny_read:
        sandbox["filesystem"] = {"denyRead": list(rules.deny_read)}
    if rules.allowed_domains is not None:
        sandbox["network"] = {
            "allowedDomains": list(rules.allowed_domains),
            "strictAllowlist": True,
            "allowManagedDomainsOnly": True,
        }
    if sandbox:
        data["sandbox"] = sandbox
    if rules.mcp is not None:
        data["allowedMcpServers"] = [_claude_server(s) for s in rules.mcp]
        data["allowManagedMcpServersOnly"] = True
    notes = []
    if rules.needs_sandbox or rules.deny_read:
        notes.append(
            "Claude Code's sandbox does not run on native Windows, so there only the Read "
            "and web rules apply; use WSL2"
        )
    return data, notes


def claude_user(
    rules: Rules, before: Mapping[str, Any], *, platform: str
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Claude Code user settings with the policy merged in: the new data, actions, guided steps.

    Only ever adds a restriction or narrows a list. Whether the result is
    really no broader is for the tighten-only test to judge.
    """
    after = copy.deepcopy(dict(before))
    actions: list[str] = []
    guided: list[str] = []
    added = _append(
        after, "permissions.deny", [r for p in rules.deny_read for r in _claude_reads(p)]
    )
    if added:
        actions.append(f"add {added} Read deny rule(s)")
    if rules.web_off and _append(after, "permissions.deny", ["WebFetch", "WebSearch"]):
        actions.append("turn off web fetch and web search")
    if platform == "win32" and (rules.needs_sandbox or rules.deny_read):
        guided.append(
            "Claude Code's sandbox does not run on native Windows: run Claude Code in WSL2 "
            "for the policy's sandbox rules"
        )
    else:
        if rules.needs_sandbox:
            turned = _set(after, ("sandbox", "enabled"), True)
            turned |= _set(after, ("sandbox", "allowUnsandboxedCommands"), False)
            if turned:
                actions.append("turn on the sandbox with no way around it")
        denied = _append(after, "sandbox.filesystem.denyRead", list(rules.deny_read))
        if denied:
            actions.append(f"deny the sandbox {denied} location(s)")
        if rules.allowed_domains is not None:
            held = after.get("sandbox", {}).get("network", {}).get("allowedDomains")
            wanted = (
                list(rules.allowed_domains)
                if not isinstance(held, list)
                else [d for d in held if d in rules.allowed_domains]
            )
            narrowed = _set(after, ("sandbox", "network", "allowedDomains"), wanted)
            narrowed |= _set(after, ("sandbox", "network", "strictAllowlist"), True)
            if narrowed:
                actions.append(f"let sandboxed commands reach only {len(wanted)} domain(s)")
    if rules.mcp is not None:
        entries = [_claude_server(s) for s in rules.mcp]
        listed = after.get("allowedMcpServers")
        servers = entries if not isinstance(listed, list) else [e for e in listed if e in entries]
        if _set(after, ("allowedMcpServers",), servers):
            actions.append(f"allow only {len(servers)} MCP server(s)")
    return after, actions, guided


# --- Codex -------------------------------------------------------------------------------


def _toml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def codex_requirements(rules: Rules) -> tuple[str, list[str]]:
    """Codex's `requirements.toml` for the policy, and what it cannot do."""
    lines: list[str] = []
    notes: list[str] = []
    if rules.needs_sandbox:
        lines.append('allowed_sandbox_modes = ["read-only", "workspace-write"]')
    if rules.web_off:
        lines.append('allowed_web_search_modes = ["disabled"]')
    if rules.deny_read:
        lines += ["", "[permissions.filesystem]", f"deny_read = {_toml_list(rules.deny_read)}"]
    if rules.allowed_domains is not None:
        lines += ["", "[experimental_network]", "enabled = true"]
        lines.append("managed_allowed_domains_only = true")
        if rules.allowed_domains:
            lines += ["", "[experimental_network.domains]"]
            lines += [f'{json.dumps(d)} = "allow"' for d in rules.allowed_domains]
    for server in rules.mcp or ():
        if not (server.url or server.command):
            notes.append(f"Codex cannot allow MCP server {server.name} by name alone")
            continue
        key, value = ("url", server.url) if server.url else ("command", server.command[0])
        lines += ["", f"[mcp_servers.{_toml_key(server.name)}]"]
        lines.append(f"identity = {{ {key} = {json.dumps(value)} }}")
        if len(server.command) > 1:
            notes.append(
                f"Codex matches MCP server {server.name} by its program, not its arguments"
            )
    if rules.mcp == ():
        notes.append("Codex requirements cannot say that no MCP server may load")
    return "\n".join(lines).strip() + "\n", notes


def _codex_admits(name: str, spec: Mapping[str, Any], allowed: Sequence[Server]) -> bool:
    command, url = spec.get("command"), spec.get("url")
    for server in allowed:
        if not (server.url or server.command) and name == server.name:
            return True
        if server.url and url == server.url:
            return True
        if server.command and isinstance(command, str) and command == server.command[0]:
            return True
    return False


def codex_user(
    rules: Rules, before: Mapping[str, Any], original: str
) -> tuple[dict[str, Any], str | None, list[str], list[str]]:
    """Codex's config.toml with the policy merged in: new data, new text, actions, guided steps.

    The text is None when there is nothing to change or the file cannot be
    edited line by line; the changes are then guided steps instead.
    """
    after = copy.deepcopy(dict(before))
    edits: list[tuple[tuple[str, ...], Any]] = []
    actions: list[str] = []
    guided: list[str] = []

    def change(keys: tuple[str, ...], value: Any, action: str) -> None:
        if _set(after, keys, value):
            edits.append((keys, value))
            actions.append(action)

    profile = str(before.get("default_permissions") or "")
    tables = before.get("permissions")
    has_profile = (
        bool(profile)
        and not profile.startswith(":")
        and isinstance(tables, Mapping)
        and isinstance(tables.get(profile), Mapping)
    )
    if rules.needs_sandbox:
        if before.get("sandbox_mode") == "danger-full-access":
            change(("sandbox_mode",), "workspace-write", "run commands in the sandbox")
        if profile == ":danger-full-access":
            guided.append("Codex's :danger-full-access profile has no sandbox: choose another")
    if rules.deny_read and has_profile:
        for path in rules.deny_read:
            change(("permissions", profile, "filesystem", path), "deny", "deny a location")
    elif rules.deny_read:
        guided.append(
            "Codex's older sandbox settings read every file: a permissions profile "
            "(default_permissions) can deny the policy's paths"
        )
    if rules.allowed_domains is not None:
        if has_profile:
            network = tables[profile].get("network") if isinstance(tables, Mapping) else None
            network = network if isinstance(network, Mapping) else {}
            if network.get("enabled") is True and not network.get("domains"):
                if not rules.allowed_domains:
                    change(
                        ("permissions", profile, "network", "enabled"),
                        False,
                        "turn off network access for sandboxed commands",
                    )
                for domain in rules.allowed_domains:
                    change(
                        ("permissions", profile, "network", "domains", domain),
                        "allow",
                        "list the domains sandboxed commands may reach",
                    )
        else:
            workspace = before.get("sandbox_workspace_write")
            if isinstance(workspace, Mapping) and workspace.get("network_access") is True:
                change(
                    ("sandbox_workspace_write", "network_access"),
                    False,
                    "turn off network access for sandboxed commands (Codex lists allowed "
                    "domains only in a permissions profile)",
                )
    if rules.web_off:
        if before.get("web_search") != "disabled":
            change(("web_search",), "disabled", "turn off web search")
        tools = before.get("tools")
        if isinstance(tools, Mapping) and tools.get("web_search") is True:
            change(("tools", "web_search"), False, "turn off web search")
    servers = before.get("mcp_servers")
    if rules.mcp is not None and isinstance(servers, Mapping):
        for name, spec in servers.items():
            if isinstance(spec, Mapping) and spec.get("enabled", True) is not False:
                if not _codex_admits(str(name), spec, rules.mcp):
                    change(("mcp_servers", str(name), "enabled"), False, "turn off an MCP server")
    actions = list(dict.fromkeys(actions))
    if not edits:
        return after, None, actions, guided
    text = _toml_edit(original, edits)
    if text is None or _toml_parse(text) != after:
        guided.append(
            "Curb could not edit Codex's config.toml safely; make these changes by hand: "
            + "; ".join(actions)
        )
        return dict(before), None, [], guided
    return after, text, actions, guided


# --- NVIDIA OpenShell ----------------------------------------------------------------------


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def openshell(rules: Rules) -> tuple[str, list[str]]:
    """An OpenShell sandbox policy for the policy, and what it cannot do.

    OpenShell lists what a sandbox may read rather than what it may not, so
    home folders such as `~/.aws` are unreadable unless listed. It enforces
    with Landlock, required here, since the policy is the sandbox.
    """
    notes: list[str] = []
    data: dict[str, Any] = {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": True,
            "read_only": list(OPENSHELL_READ_ONLY),
            "read_write": list(OPENSHELL_READ_WRITE),
        },
        "landlock": {"compatibility": "hard_requirement"},
    }
    roots = OPENSHELL_READ_ONLY + OPENSHELL_READ_WRITE
    if any(_inside(p, r) for p in rules.deny_read for r in roots):
        notes.append("OpenShell cannot deny a path inside a folder its policy allows")
    policies: dict[str, Any] = {}
    for number, domain in enumerate(rules.allowed_domains or (), start=1):
        policies[f"curb_allowed_{number}"] = {
            "endpoints": [{"host": domain, "port": 443, "enforcement": "enforce"}]
        }
    for server in rules.mcp or ():
        if server.url:
            parts = urlsplit(server.url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            policies[f"curb_mcp_{re.sub(r'[^a-z0-9_]', '_', server.name.lower())}"] = {
                "endpoints": [
                    {
                        "host": parts.hostname,
                        "port": port,
                        "protocol": "mcp",
                        "enforcement": "enforce",
                    }
                ]
            }
        else:
            notes.append(
                f"MCP server {server.name} runs in the sandbox; OpenShell does not list programs"
            )
    if policies:
        data["network_policies"] = policies
    if rules.web_off:
        notes.append("web search runs at the model provider, outside OpenShell's reach")
    return yaml.safe_dump(data, sort_keys=False), notes
