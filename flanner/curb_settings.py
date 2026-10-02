"""What a Claude Code or Codex launch would apply, read from its settings files.

Resolution only: which files a launch loads, in what order, and what the
merged result says about permissions, the sandbox, MCP servers and hooks.
Deciding what that means for reach is `curb_reach`'s job.

Every value records where it came from, because the report has to say who
controls each item (the user, the project, or an admin) and because a
setting Curb could not read must count as no control at all (PRD §9.4).
Nothing here runs a program, follows a network address or prints a value:
the files are parsed, and only structure and names are kept.

Sources: the Claude Code settings, permissions, sandboxing and managed MCP
docs, and the Codex config reference, as cited in the PRD (E11-E17, E47).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agent_paths
from .curb_context import CLAUDE, CODEX, LaunchContext

ADMIN, USER, PROJECT = "admin", "user", "project"

#: Claude Code's scalar precedence, highest first (E11).
CLAUDE_ORDER = ("managed", "cli", "local", "project", "user")


@dataclass(frozen=True)
class Layer:
    """One settings source a launch loads, or would have loaded."""

    name: str
    where: str
    controller: str
    present: bool
    data: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    #: Where a `/path` permission rule, or a relative sandbox path, resolves.
    anchor: Path | None = None

    @property
    def usable(self) -> bool:
        return self.present and self.error is None


@dataclass(frozen=True)
class Rule:
    """A Claude Code permission rule, with the directory its `/path` form anchors at."""

    text: str
    tool: str
    spec: str | None
    source: str
    anchor: Path


@dataclass(frozen=True)
class McpServer:
    name: str
    transport: str
    command: tuple[str, ...] = ()
    url: str | None = None
    env_names: tuple[str, ...] = ()
    source: str = ""
    controller: str = USER


@dataclass(frozen=True)
class HookSet:
    event: str
    count: int
    source: str
    controller: str


@dataclass
class ClaudeSettings:
    layers: list[Layer]
    deny: list[Rule]
    allow: list[Rule]
    mode: str
    mode_source: str
    sandbox_enabled: bool
    filesystem_disabled: bool
    deny_read: list[tuple[str, Path]]
    allow_read: list[tuple[str, Path]]
    credential_files: list[tuple[str, Path]]
    credential_env: list[str]
    allow_unsandboxed: bool
    excluded_commands: list[str]
    strict_allowlist: bool
    managed_domains_only: bool
    allowed_domains: list[str]
    admin_required: bool
    block_reads_outside: bool
    working_dirs: list[Path]
    mcp: list[McpServer]
    hooks: list[HookSet]
    #: Things the settings could not answer, said plainly in the report.
    not_checked: list[str]
    #: Values Curb had to assume, each counted as the worse case.
    assumed: list[str]
    #: Values taken from an agent's documented default rather than a file.
    defaults: list[str] = field(default_factory=list)


@dataclass
class CodexSettings:
    layers: list[Layer]
    trusted: bool | None
    sandbox: str
    sandbox_assumed: bool
    approval: str
    approval_assumed: bool
    reviewer_auto: bool
    network_access: bool
    network_domains: dict[str, str]
    proxy_enabled: bool
    deny_read: list[str]
    web_search: str
    web_search_assumed: bool
    mcp: list[McpServer]
    apps: bool | None
    hooks: list[HookSet]
    not_checked: list[str]
    assumed: list[str]
    defaults: list[str] = field(default_factory=list)
    #: shell_environment_policy: which variables reach the commands Codex runs.
    env_inherit: str = "all"
    env_keep_secret_names: bool = True
    env_filters: dict[str, str] = field(default_factory=dict)
    env_include_only: list[str] = field(default_factory=list)


# --- reading files -------------------------------------------------------------


def _toml_loads() -> Callable[[str], dict[str, Any]] | None:
    """`tomllib.loads`, or None on Python 3.10, which ships no TOML reader."""
    try:
        loads: Callable[[str], dict[str, Any]] = importlib.import_module("tomllib").loads
    except ModuleNotFoundError:
        return None
    return loads


def _read_json(path: Path) -> tuple[bool, dict[str, Any], str | None]:
    if not path.is_file():
        return False, {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError) as error:
        return True, {}, f"could not be read ({type(error).__name__})"
    if not isinstance(data, dict):
        return True, {}, "is not a JSON object"
    return True, data, None


def _read_toml(path: Path) -> tuple[bool, dict[str, Any], str | None]:
    if not path.is_file():
        return False, {}, None
    loads = _toml_loads()
    if loads is None:
        return True, {}, "could not be read: Python 3.10 has no TOML reader"
    try:
        return True, loads(path.read_text(encoding="utf-8-sig")), None
    except (OSError, ValueError) as error:
        return True, {}, f"could not be read ({type(error).__name__})"


def _get(data: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def norm_key(path: str | Path) -> str:
    """A path as a comparable key: no `\\\\?\\` prefix, forward slashes, and on
    Windows lower case, because both agents record the same folder spelled
    several ways."""
    text = str(path).replace("\\", "/")
    if text.startswith("//?/"):
        text = text[4:]
    text = text.rstrip("/") or "/"
    return text.lower() if sys.platform == "win32" or (len(text) > 1 and text[1] == ":") else text


def _within(child: Path, parent_key: str) -> bool:
    key = norm_key(child)
    return key == parent_key or key.startswith(parent_key.rstrip("/") + "/")


# --- Claude Code --------------------------------------------------------------


def system_root() -> Path:
    """The filesystem root admin files are read under.

    `FLANNER_CURB_SYSTEM_ROOT` moves it, so the test suite never reads the
    machine's real managed settings.
    """
    moved = os.environ.get("FLANNER_CURB_SYSTEM_ROOT")
    return Path(moved) if moved else Path(Path.cwd().anchor)


def managed_dir(platform: str, root: Path) -> Path:
    """Where Claude Code looks for admin-deployed files on this platform."""
    if platform == "darwin":
        return root / "Library" / "Application Support" / "ClaudeCode"
    if platform == "win32":
        program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        relative = Path(program_files).relative_to(Path(program_files).anchor)
        return root / relative / "ClaudeCode"
    return root / "etc" / "claude-code"


def _claude_managed(platform: str, root: Path, cwd: Path) -> Layer:
    base = managed_dir(platform, root)
    present, data, error = _read_json(base / "managed-settings.json")
    drop_ins = sorted((base / "managed-settings.d").glob("*.json")) if base.is_dir() else []
    merged: dict[str, Any] = dict(data)
    for extra in drop_ins:
        found, more, problem = _read_json(extra)
        if found and problem is None:
            merged = _deep_merge(merged, more)
            present = True
        elif found:
            error = error or f"{extra.name} {problem}"
    return Layer("managed", str(base), ADMIN, present, merged, error, cwd)


def _claude_cli_layers(context: LaunchContext) -> list[Layer]:
    layers = []
    for value in context.settings:
        text = value.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                layers.append(Layer("cli", "--settings (inline)", USER, True, {}, "is not JSON"))
                continue
            layers.append(Layer("cli", "--settings (inline)", USER, True, data, None, context.cwd))
            continue
        path = Path(text) if Path(text).is_absolute() else context.cwd / text
        present, data, error = _read_json(path)
        if not present:
            error = "was not found"
        layers.append(Layer("cli", str(path), USER, True, data, error, path.parent))
    return layers


def _deep_merge(low: Mapping[str, Any], high: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(low)
    for key, value in high.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_rule(text: str, source: str, anchor: Path) -> Rule:
    text = text.strip()
    if "(" in text and text.endswith(")"):
        tool, spec = text.split("(", 1)
        return Rule(text, tool.strip(), spec[:-1], source, anchor)
    return Rule(text, text, None, source, anchor)


def resolve_claude(
    context: LaunchContext,
    *,
    platform: str | None = None,
    root: Path | None = None,
) -> ClaudeSettings:
    """Everything a Claude Code launch would load, merged the way it merges it."""
    platform = platform or sys.platform
    root = root or system_root()
    config_dir = agent_paths.claude_config_dir()
    cwd = context.cwd
    not_checked = [
        "Managed settings delivered by MDM, the registry or the claude.ai console",
        "Plugin-provided MCP servers and hooks",
        "claude.ai connectors",
    ]
    assumed: list[str] = []

    managed = _claude_managed(platform, root, cwd)
    layers: list[Layer] = [managed, *_claude_cli_layers(context)]
    sources = set(context.setting_sources or ("user", "project", "local"))
    if context.restricted:
        sources = set()
    for name, path, controller, anchor in (
        ("local", cwd / ".claude" / "settings.local.json", PROJECT, cwd),
        ("project", cwd / ".claude" / "settings.json", PROJECT, cwd),
        ("user", config_dir / "settings.json", USER, config_dir),
    ):
        present, data, error = _read_json(path)
        if name not in sources:
            layers.append(Layer(name, f"{path} (not loaded by this launch)", controller, False))
            continue
        layers.append(Layer(name, str(path), controller, present, data, error, anchor))
    for layer in layers:
        if layer.present and layer.error:
            assumed.append(f"{layer.where} {layer.error}, so every channel counts as unknown")

    usable = [layer for layer in layers if layer.usable]
    by_name: dict[str, list[Layer]] = {}
    for layer in usable:
        by_name.setdefault(layer.name, []).append(layer)

    def ordered() -> Iterable[Layer]:
        for name in CLAUDE_ORDER:
            yield from by_name.get(name, [])

    def scalar(key: str, *, honoured: tuple[str, ...] = CLAUDE_ORDER) -> tuple[Any, str]:
        for layer in ordered():
            if layer.name in honoured:
                value = _get(layer.data, key)
                if value is not None:
                    return value, layer.name
        return None, ""

    # Permission rules: deny from every source applies (E13). One managed key
    # makes managed settings the only source of rules.
    managed_only = bool(_get(managed.data, "permissions.allowManagedPermissionRulesOnly"))
    deny: list[Rule] = []
    allow: list[Rule] = []
    for layer in ordered():
        if managed_only and layer.name != "managed":
            continue
        anchor = layer.anchor or cwd
        for text in _strings(_get(layer.data, "permissions.deny")):
            deny.append(_parse_rule(text, layer.name, anchor))
        for text in _strings(_get(layer.data, "permissions.allow")):
            allow.append(_parse_rule(text, layer.name, anchor))
    if not managed_only:
        deny.extend(_parse_rule(text, "cli", cwd) for text in context.disallowed)

    # The permission mode a session starts in.
    mode, mode_source = context.permission_mode, "launch flag" if context.permission_mode else ""
    if mode is None:
        for layer in ordered():
            value = _get(layer.data, "permissions.defaultMode")
            if not isinstance(value, str):
                continue
            if value in ("auto", "bypassPermissions") and layer.name in ("project", "local"):
                continue  # ignored from project files since v2.1.257 (E11)
            mode, mode_source = value, layer.name
            break
    mode = str(mode) if mode and mode != "manual" else "default"
    bypass_off = any(
        _get(layer.data, "permissions.disableBypassPermissionsMode") == "disable"
        for layer in usable
    )
    if mode == "bypassPermissions" and bypass_off:
        mode, mode_source = "default", "disableBypassPermissionsMode"
    if mode == "auto" and any(
        _get(layer.data, "permissions.disableAutoMode") == "disable" for layer in usable
    ):
        mode, mode_source = "default", "disableAutoMode"

    # The sandbox (E12).
    enabled, _ = scalar("sandbox.enabled")
    trusted = ("managed", "user", "cli")
    managed_fs = _get(managed.data, "sandbox.filesystem") is not None
    fs_disabled, _ = scalar(
        "sandbox.filesystem.disabled", honoured=("managed",) if managed_fs else trusted
    )
    strict_false = any(
        _get(layer.data, "sandbox.allowUnsandboxedCommands") is False
        for layer in usable
        if layer.name in trusted
    )
    allow_unsandboxed_value, _ = scalar("sandbox.allowUnsandboxedCommands")
    allow_unsandboxed = not strict_false and allow_unsandboxed_value is not False
    admin_required = any(
        _get(layer.data, "sandbox.allowUnsandboxedCommands") is False
        for layer in usable
        if layer.name in ("managed", "cli")
    )

    deny_read: list[tuple[str, Path]] = []
    allow_read: list[tuple[str, Path]] = []
    cred_files: list[tuple[str, Path]] = []
    cred_env: list[str] = []
    excluded: list[str] = []
    allowed_domains: list[str] = []
    for layer in usable:
        anchor = layer.anchor or cwd
        repo_file = layer.name in ("project", "local")
        deny_read += [
            (p, anchor) for p in _strings(_get(layer.data, "sandbox.filesystem.denyRead"))
        ]
        if not (admin_required and repo_file):
            allow_read += [
                (p, anchor) for p in _strings(_get(layer.data, "sandbox.filesystem.allowRead"))
            ]
            excluded += _strings(_get(layer.data, "sandbox.excludedCommands"))
            allowed_domains += _strings(_get(layer.data, "sandbox.network.allowedDomains"))
        for entry in _get(layer.data, "sandbox.credentials.files") or []:
            if isinstance(entry, Mapping) and entry.get("mode") in ("deny", "mask"):
                cred_files.append((str(entry.get("path", "")), anchor))
        for entry in _get(layer.data, "sandbox.credentials.envVars") or []:
            if not isinstance(entry, Mapping):
                continue
            honoured = entry.get("mode") == "deny" or (
                entry.get("mode") == "mask" and layer.name in trusted
            )
            if honoured and entry.get("name"):
                cred_env.append(str(entry["name"]))
    managed_domains_only = bool(_get(managed.data, "sandbox.network.allowManagedDomainsOnly"))
    for rule in allow:
        if rule.tool == "WebFetch" and rule.spec and rule.spec.startswith("domain:"):
            if not managed_domains_only or rule.source == "managed":
                allowed_domains.append(rule.spec[len("domain:") :])
    strict_allowlist, _ = scalar("sandbox.network.strictAllowlist", honoured=trusted)

    block_reads, _ = scalar("permissions.blockReadsOutsideWorkingDirectories")
    working = [cwd]
    for layer in usable:
        for extra in _strings(_get(layer.data, "permissions.additionalDirectories")):
            working.append(Path(os.path.expanduser(extra)))
    working += [Path(os.path.expanduser(extra)) for extra in context.add_dirs]

    mcp, mcp_notes = _claude_mcp(context, layers, managed, platform, root)
    not_checked += mcp_notes
    hooks = [] if (context.bare or context.safe_mode) else _hooks(usable)

    return ClaudeSettings(
        layers=layers,
        deny=deny,
        allow=allow,
        mode=mode,
        mode_source=mode_source or "default",
        sandbox_enabled=enabled is True,
        filesystem_disabled=fs_disabled is True,
        deny_read=deny_read,
        allow_read=allow_read,
        credential_files=cred_files,
        credential_env=cred_env,
        allow_unsandboxed=allow_unsandboxed,
        excluded_commands=excluded,
        strict_allowlist=strict_allowlist is True,
        managed_domains_only=managed_domains_only,
        allowed_domains=allowed_domains,
        admin_required=admin_required,
        block_reads_outside=block_reads is True,
        working_dirs=working,
        mcp=mcp,
        hooks=hooks,
        not_checked=not_checked,
        assumed=assumed,
    )


def _servers(raw: Any, source: str, controller: str) -> list[McpServer]:
    if not isinstance(raw, Mapping):
        return []
    found = []
    for name, spec in raw.items():
        if not isinstance(spec, Mapping):
            continue
        url = spec.get("url") if isinstance(spec.get("url"), str) else None
        kind = str(spec.get("type") or ("http" if url else "stdio"))
        if kind == "streamable-http":
            kind = "http"
        command = (
            tuple([str(spec["command"])] + _strings(spec.get("args")))
            if spec.get("command")
            else ()
        )
        raw_env = spec.get("env")
        env = raw_env if isinstance(raw_env, Mapping) else {}
        found.append(
            McpServer(
                name=str(name),
                transport=kind,
                command=command,
                url=url,
                env_names=tuple(sorted(str(key) for key in env)),
                source=source,
                controller=controller,
            )
        )
    return found


def _claude_mcp(
    context: LaunchContext,
    layers: list[Layer],
    managed: Layer,
    platform: str,
    system_root: Path,
) -> tuple[list[McpServer], list[str]]:
    """The MCP servers a launch would load, after the admin's allow and deny lists (E14)."""
    notes: list[str] = []
    usable = [layer for layer in layers if layer.usable]
    provided = _servers(_get(managed.data, "managedMcpServers"), "managed settings", ADMIN)
    exclusive_path = managed_dir(platform, system_root) / "managed-mcp.json"
    found, exclusive, problem = _read_json(exclusive_path)
    servers: list[McpServer] = []
    if found and problem is None:
        servers = _servers(exclusive.get("mcpServers"), "managed-mcp.json", ADMIN) + provided
        if context.mcp_configs:
            notes.append("This launch passes --mcp-config while managed-mcp.json is deployed")
    elif context.safe_mode:
        servers = provided
    elif context.strict_mcp:
        servers = _config_servers(context)
    else:
        user_file = agent_paths.claude_user_config()
        _, user_data, user_error = _read_json(user_file)
        if user_error:
            notes.append(f"{user_file.name} {user_error}")
        servers += _servers(user_data.get("mcpServers"), "user (~/.claude.json)", USER)
        for layer in usable:
            if layer.name == "user":
                servers += _servers(layer.data.get("mcpServers"), "user settings", USER)
        project_entry: Mapping[str, Any] = {}
        projects = user_data.get("projects")
        if isinstance(projects, Mapping):
            key = norm_key(context.cwd)
            for recorded, entry in projects.items():
                if norm_key(recorded) == key and isinstance(entry, Mapping):
                    project_entry = entry
        _, mcp_json, mcp_error = _read_json(context.cwd / ".mcp.json")
        if mcp_error:
            notes.append(f".mcp.json {mcp_error}")
        disabled = set(_strings(project_entry.get("disabledMcpjsonServers")))
        for layer in usable:
            disabled |= set(_strings(layer.data.get("disabledMcpjsonServers")))
        servers += [
            s
            for s in _servers(mcp_json.get("mcpServers"), ".mcp.json", PROJECT)
            if s.name not in disabled
        ]
        servers += _servers(project_entry.get("mcpServers"), "local (~/.claude.json)", USER)
        servers += provided + _config_servers(context)

    # One server per name, the highest-precedence definition winning: the
    # list above runs from user scope up to the admin's provided servers.
    servers = list({server.name: server for server in servers}.values())
    denied = [entry for layer in usable for entry in _list(layer.data, "deniedMcpServers")]
    managed_allow_only = bool(managed.data.get("allowManagedMcpServersOnly"))
    allowed: list[Mapping[str, Any]] | None = None
    for layer in usable:
        if managed_allow_only and layer.name != "managed":
            continue
        entries = layer.data.get("allowedMcpServers")
        if isinstance(entries, list):
            allowed = (allowed or []) + [e for e in entries if isinstance(e, Mapping)]
    kept = []
    for server in servers:
        if any(_matches(server, entry) for entry in denied):
            continue
        if allowed is not None and server.controller != ADMIN and not _allowed(server, allowed):
            continue
        kept.append(server)
    return kept, notes


def _config_servers(context: LaunchContext) -> list[McpServer]:
    servers: list[McpServer] = []
    for value in context.mcp_configs:
        text = value.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                continue
        else:
            path = Path(text) if Path(text).is_absolute() else context.cwd / text
            _, data, _ = _read_json(path)
        servers += _servers(data.get("mcpServers"), "--mcp-config", USER)
    return servers


def _list(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = data.get(key)
    return (
        [entry for entry in value if isinstance(entry, Mapping)] if isinstance(value, list) else []
    )


def _url_matches(pattern: str, url: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(url.lower().rstrip("/"), pattern.lower().rstrip("/")) or (
        "/" not in pattern.split("://", 1)[-1]
        and fnmatch.fnmatchcase(
            url.lower().split("://", 1)[-1].split("/", 1)[0], pattern.lower().split("://", 1)[-1]
        )
    )


def _matches(server: McpServer, entry: Mapping[str, Any]) -> bool:
    if "serverName" in entry:
        return bool(entry["serverName"] == server.name)
    if "serverCommand" in entry:
        return list(server.command) == _strings(entry["serverCommand"])
    if "serverUrl" in entry and server.url:
        return _url_matches(str(entry["serverUrl"]), server.url)
    return False


def _allowed(server: McpServer, entries: list[Mapping[str, Any]]) -> bool:
    """Whether an allowlist admits a server, by the type rules in E14.

    A name entry counts for a stdio server only while the list has no
    command entries, and for a remote server only while it has no URL
    entries: a name is a label anybody can give any server.
    """
    remote = server.transport in ("http", "sse")
    exact = "serverUrl" if remote else "serverCommand"
    if any(exact in entry for entry in entries):
        return any(exact in entry and _matches(server, entry) for entry in entries)
    return any("serverName" in entry and _matches(server, entry) for entry in entries)


def _hooks(layers: Iterable[Layer]) -> list[HookSet]:
    found = []
    for layer in layers:
        hooks = layer.data.get("hooks")
        if not isinstance(hooks, Mapping):
            continue
        for event, groups in hooks.items():
            count = 0
            for group in groups if isinstance(groups, list) else []:
                inner = group.get("hooks") if isinstance(group, Mapping) else None
                count += len(inner) if isinstance(inner, list) else 1
            if count:
                found.append(HookSet(str(event), count, layer.name, layer.controller))
    return found


# --- Codex --------------------------------------------------------------------


def _parse_toml_value(raw: str) -> Any:
    """A `-c key=value` value the way Codex reads it: TOML, else a plain string."""
    loads = _toml_loads()
    if loads is not None:
        try:
            return loads(f"v = {raw}")["v"]
        except ValueError:
            pass
    return raw


def _set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = [part.strip().strip('"') for part in dotted.split(".")]
    current = data
    for part in parts[:-1]:
        nxt = current.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            current[part] = nxt
        current = nxt
    current[parts[-1]] = value


def resolve_codex(
    context: LaunchContext,
    *,
    platform: str | None = None,
    root: Path | None = None,
) -> CodexSettings:
    """Everything a Codex launch would load, merged in its documented order (E47)."""
    platform = platform or sys.platform
    root = root or system_root()
    home = agent_paths.codex_home()
    not_checked = ["Cloud-managed defaults", "ChatGPT apps connected to the account"]
    assumed: list[str] = []
    defaults: list[str] = []
    layers: list[Layer] = []

    # /etc/codex is a Unix location; Windows has no documented equivalent.
    system_dir = root / "etc" / "codex"
    unix = platform != "win32"
    if not unix:
        not_checked.append("System config and requirements.toml on Windows")
    present, data, error = _read_toml(system_dir / "config.toml") if unix else (False, {}, None)
    layers.append(Layer("system", str(system_dir / "config.toml"), ADMIN, present, data, error))

    user_path = home / "config.toml"
    present, user_data, error = _read_toml(user_path)
    if context.ignore_user_config:
        layers.append(Layer("user", f"{user_path} (not loaded by this launch)", USER, False))
    else:
        layers.append(Layer("user", str(user_path), USER, present, user_data, error))

    trusted = _trust(user_data, context.cwd)
    if context.profile:
        legacy = _get(user_data, f"profiles.{context.profile}")
        if isinstance(legacy, Mapping):
            layers.append(Layer("profile", f"[profiles.{context.profile}]", USER, True, legacy))
        profile_path = home / f"{context.profile}.config.toml"
        present, data, error = _read_toml(profile_path)
        layers.append(Layer("profile", str(profile_path), USER, present, data, error))
        if not present and not isinstance(legacy, Mapping):
            assumed.append(f"Profile {context.profile!r} was not found")

    for project_dir in _ancestors(context.cwd):
        path = project_dir / ".codex" / "config.toml"
        if path.is_file():
            present, data, error = _read_toml(path)
            loads = trusted is True
            label = str(path) if loads else f"{path} (skipped: project not trusted)"
            layers.append(Layer("project", label, PROJECT, present and loads, data, error))
            break

    cli: dict[str, Any] = {}
    for key, raw in context.overrides:
        _set_dotted(cli, key, _parse_toml_value(raw))
    if context.overrides:
        layers.append(Layer("cli", "-c overrides", USER, True, cli))

    present, requirements, error = (
        _read_toml(system_dir / "requirements.toml") if unix else (False, {}, None)
    )
    layers.append(
        Layer(
            "requirements",
            str(system_dir / "requirements.toml"),
            ADMIN,
            present,
            requirements,
            error,
        )
    )
    for layer in layers:
        if layer.present and layer.error:
            assumed.append(f"{layer.where} {layer.error}, so every channel counts as unknown")

    merged: dict[str, Any] = {}
    for layer in layers:
        if layer.usable and layer.name != "requirements":
            merged = _deep_merge(merged, layer.data)

    # The sandbox and approvals.
    sandbox_assumed = False
    sandbox = context.sandbox or merged.get("sandbox_mode")
    if not sandbox:
        builtin = {
            ":read-only": "read-only",
            ":workspace": "workspace-write",
            ":danger-full-access": "danger-full-access",
        }
        sandbox = builtin.get(str(merged.get("default_permissions", "")), "")
    if context.approve_for_me:
        sandbox = sandbox or "workspace-write"
    if not sandbox:
        sandbox, sandbox_assumed = "workspace-write", True
        defaults.append("Codex's default sandbox (assumed workspace-write)")
    approval: Any = context.approval or merged.get("approval_policy")
    approval_assumed = False
    if isinstance(approval, Mapping):
        raw_granular = approval.get("granular")
        granular = raw_granular if isinstance(raw_granular, Mapping) else {}
        approval = "never" if granular.get("sandbox_approval") is False else "on-request"
    if not approval:
        approval, approval_assumed = "on-request", True
        defaults.append("Codex's default approval policy (assumed on-request)")
    if context.bypass:
        sandbox, approval = "danger-full-access", "never"
    reviewer_auto = context.approve_for_me or merged.get("approvals_reviewer") == "auto_review"

    # Files and network.
    profile_name = str(merged.get("default_permissions", ""))
    profile = (
        _get(merged, f"permissions.{profile_name}")
        if profile_name and not profile_name.startswith(":")
        else None
    )
    deny_read = _strings(_get(requirements, "permissions.filesystem.deny_read"))
    network_domains: dict[str, str] = {}
    network_on = bool(_get(merged, "sandbox_workspace_write.network_access", False))
    if isinstance(profile, Mapping):
        for path, rule in (_get(profile, "filesystem") or {}).items():
            if rule == "deny":
                deny_read.append(str(path))
        net = _get(profile, "network")
        if isinstance(net, Mapping):
            network_on = bool(net.get("enabled", network_on))
            for pattern, verdict in (net.get("domains") or {}).items():
                network_domains[str(pattern)] = str(verdict)
    proxy = (
        merged.get("features", {}).get("network_proxy")
        if isinstance(merged.get("features"), Mapping)
        else None
    )
    proxy_enabled = bool(proxy if isinstance(proxy, bool) else _get(proxy or {}, "enabled", False))
    if isinstance(proxy, Mapping):
        for pattern, verdict in (proxy.get("domains") or {}).items():
            network_domains.setdefault(str(pattern), str(verdict))

    # Web search.
    web_assumed = False
    web = "live" if context.search else merged.get("web_search")
    if web is None:
        legacy_tool = _get(merged, "tools.web_search")
        if legacy_tool is True or isinstance(legacy_tool, Mapping):
            web = "live"
        elif legacy_tool is False:
            web = "disabled"
    if web is None:
        web, web_assumed = "cached", True
        defaults.append("Codex's default web search (assumed cached)")
    allowed_web = _strings(requirements.get("allowed_web_search_modes"))
    if allowed_web and web not in allowed_web:
        assumed.append(f"web_search {web!r} is outside the admin's allowed modes")

    mcp = [
        server
        for server in _codex_servers(merged)
        if server.name not in set(_strings(requirements.get("disabled_mcp_servers")))
    ]
    # Apps are a stable feature, on by default in 0.154.0 (`codex features
    # list`). Which apps are connected lives in the ChatGPT account.
    raw_apps = merged.get("apps")
    table: Mapping[str, Any] = raw_apps if isinstance(raw_apps, Mapping) else {}
    every = _get(table, "_default.enabled")
    apps: bool | None
    if _get(merged, "features.apps") is False:
        apps = False
    elif every is True or any(
        isinstance(v, Mapping) and v.get("enabled") is True
        for k, v in table.items()
        if k != "_default"
    ):
        apps = True
    elif every is False:
        apps = False
    else:
        apps = None
        defaults.append("Codex apps (on by default; connected apps are assumed)")
    hooks = _codex_hooks(merged, home)
    policy = merged.get("shell_environment_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    filters = {str(k): str(v) for k, v in (policy.get("filters") or {}).items()}
    for pattern in _strings(policy.get("exclude")):
        filters.setdefault(pattern, "exclude")
    return CodexSettings(
        layers=layers,
        trusted=trusted,
        sandbox=str(sandbox),
        sandbox_assumed=sandbox_assumed,
        approval=str(approval),
        approval_assumed=approval_assumed,
        reviewer_auto=bool(reviewer_auto),
        network_access=network_on,
        network_domains=network_domains,
        proxy_enabled=proxy_enabled,
        deny_read=deny_read,
        web_search=str(web),
        web_search_assumed=web_assumed,
        mcp=mcp,
        apps=apps,
        hooks=hooks,
        not_checked=not_checked,
        assumed=assumed,
        defaults=defaults,
        env_inherit=str(policy.get("inherit", "all")),
        env_keep_secret_names=policy.get("ignore_default_excludes", True) is not False,
        env_filters=filters,
        env_include_only=_strings(policy.get("include_only")),
    )


def _ancestors(start: Path) -> list[Path]:
    return [start, *start.parents]


def _trust(user: Mapping[str, Any], cwd: Path) -> bool | None:
    """Whether Codex trusts this folder: the nearest recorded ancestor decides."""
    projects = user.get("projects")
    if not isinstance(projects, Mapping):
        return None
    best: tuple[int, bool] | None = None
    for recorded, entry in projects.items():
        if not isinstance(entry, Mapping):
            continue
        key = norm_key(recorded)
        if _within(cwd, key):
            level = entry.get("trust_level")
            if level in ("trusted", "untrusted") and (best is None or len(key) > best[0]):
                best = (len(key), level == "trusted")
    return None if best is None else best[1]


def _codex_servers(merged: Mapping[str, Any]) -> list[McpServer]:
    raw = merged.get("mcp_servers")
    if not isinstance(raw, Mapping):
        return []
    enabled = {
        name: spec
        for name, spec in raw.items()
        if isinstance(spec, Mapping) and spec.get("enabled", True) is not False
    }
    return _servers(enabled, "config.toml", USER)


#: Codex's hook events (E17). Other keys under `[hooks]`, such as the trust
#: state Codex keeps there, are not hooks.
CODEX_HOOK_EVENTS = frozenset(
    {
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SessionStart",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
        "UserPromptSubmit",
        "Stop",
        "Interrupt",
    }
)


def _codex_hooks(merged: Mapping[str, Any], home: Path) -> list[HookSet]:
    found: list[HookSet] = []
    present, data, _ = _read_json(home / "hooks.json")
    for where, table in (("config.toml", merged.get("hooks")), ("hooks.json", data.get("hooks"))):
        if not isinstance(table, Mapping):
            continue
        for event, groups in table.items():
            if event in CODEX_HOOK_EVENTS:
                count = len(groups) if isinstance(groups, list) else 1
                found.append(HookSet(str(event), count, where, USER))
    return found


def resolve(context: LaunchContext, **kwargs: Any) -> ClaudeSettings | CodexSettings:
    if context.agent == CLAUDE:
        return resolve_claude(context, **kwargs)
    if context.agent == CODEX:
        return resolve_codex(context, **kwargs)
    raise ValueError(context.agent)
