"""Fixes: settings changes that close open channels, written only with a grant (Curb PRD §10.4).

A fix goes into the agent's own user settings, never a project's or the
admin's, so it follows the person into every project and they can see it.
Each one is planned from a `curb map` assessment and must pass the
tighten-only test (§10.8, ADR 0007); anything that fails it is left for a
person, with the reason. Applying needs a grant for exactly that change
(§11.2, ADR 0006):

1. each file is copied to `~/.flanner/curb/backups/`, user-only;
2. the new text is written in one step, read back and compared with what
   was meant, and any difference puts every file back;
3. `flanner curb fix --undo` restores the latest backed-up files, while they
   still hold what Curb wrote.

A settings file can hold tokens, so every fix also denies each supported
agent Curb's backup folder. Backups are kept seven days.

Codex's TOML is edited as text, line by line, so comments survive, and the
result is parsed and compared with the intended data before anything is
written. An edit Curb cannot make that way becomes guided steps instead.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agent_paths, curb_approval, curb_reach, curb_store, curb_tighten
from .curb_approval import Broker, Grant
from .curb_context import CLAUDE, CODEX, LABELS
from .curb_credentials import Credential
from .curb_match import posix
from .curb_reach import AgentReport
from .curb_settings import ClaudeSettings, CodexSettings

BACKUP_DAYS = 7


class FixFailed(RuntimeError):
    """A written file did not read back as meant; every file was put back."""


@dataclass(frozen=True)
class Edit:
    agent: str
    path: Path
    before: dict[str, Any]
    after: dict[str, Any]
    text: str
    #: What it does, in words that name no credential and no location.
    actions: tuple[str, ...]

    @property
    def where(self) -> str:
        return f"{LABELS[self.agent]} user settings"


@dataclass
class Plan:
    edits: list[Edit] = field(default_factory=list)
    guided: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def change(self) -> dict[str, str]:
        """The exact change a grant binds to: each file's new text."""
        return {str(edit.path): edit.text for edit in self.edits}

    def summary(self) -> str:
        return "; ".join(f"{e.where}: {', '.join(e.actions)}" for e in self.edits)


def backups_dir() -> Path:
    return curb_store.curb_dir() / "backups"


# --- planning ----------------------------------------------------------------------------


def plan(
    reports: Sequence[AgentReport],
    *,
    home: Path,
    platform: str,
    env: Mapping[str, str],
) -> Plan:
    """Fixes for each agent's default launch, each one tighten-only or left out.

    When any fix will be written, every agent is also denied Curb's backup
    folder, since the backups are copies of settings files.
    """
    out = Plan()
    defaults = {r.context.agent: r for r in reports if r.context.source == "default"}
    planned: dict[str, Edit] = {}
    for agent, report in defaults.items():
        edit, guided = _planned(report, home=home, platform=platform, backups_only=False)
        out.guided += guided
        if edit is not None:
            planned[agent] = edit
    if planned:
        for agent, report in defaults.items():
            if agent not in planned:
                edit, _ = _planned(report, home=home, platform=platform, backups_only=True)
                if edit is not None:
                    planned[agent] = edit
    for agent, edit in planned.items():
        report = defaults[agent]
        verdict = curb_tighten.check(
            report.context,
            edit.path,
            edit.after,
            probes=_probes(report, home),
            platform=platform,
            home=home,
            env=env,
        )
        if verdict.tighten_only:
            out.edits.append(edit)
        else:
            reasons = "; ".join(verdict.broader + verdict.unproven)
            out.refused.append(f"{edit.where}: not applied, because {reasons}")
    return out


def _planned(
    report: AgentReport, *, home: Path, platform: str, backups_only: bool
) -> tuple[Edit | None, list[str]]:
    if isinstance(report.settings, ClaudeSettings):
        path = agent_paths.claude_config_dir() / "settings.json"
        return _claude(report, report.settings, path, home, platform, backups_only)
    path = agent_paths.codex_home() / "config.toml"
    return _codex(report, report.settings, path, home, backups_only)


def _probes(report: AgentReport, home: Path) -> list[Credential]:
    found = [r.credential for r in report.reach]
    extra = (
        home / ".curb-probe",
        report.context.cwd / ".curb-probe",
        backups_dir() / "probe",
    )
    return found + [Credential("probe", "probe", "Probe", (path,)) for path in extra]


def _under(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _claude_rule(path: Path, home: Path, cwd: Path) -> str:
    """A Read rule for a credential: its folder, or the file when it sits in home."""
    if _under(path, cwd) is not None:
        return f"Read({path.name})"  # a bare name matches at any depth
    relative = _under(path, home)
    if relative is not None:
        if len(relative.parts) == 1:
            return f"Read(~/{relative.as_posix()})"
        return f"Read(~/{relative.parent.as_posix()}/**)"
    return "Read(/" + posix(path.parent) + "/**)"


def _home_form(path: Path, home: Path) -> str:
    relative = _under(path, home)
    return f"~/{relative.as_posix()}" if relative is not None else str(path)


def _sandbox_entry(path: Path, home: Path, cwd: Path) -> str:
    """A sandbox entry for a credential: its folder in home, or the file itself."""
    relative = _under(path, home)
    if relative is not None and len(relative.parts) > 1 and _under(path, cwd) is None:
        return _home_form(path.parent, home)
    return _home_form(path, home)


def _append(target: dict[str, Any], dotted: str, values: Sequence[Any]) -> int:
    """Add values to a list setting, skipping any already there. Returns how many were new."""
    *parents, key = dotted.split(".")
    node = target
    for part in parents:
        node = node.setdefault(part, {})
    held = node.setdefault(key, [])
    new = [v for v in values if v not in held]
    held.extend(new)
    return len(new)


def _set(target: dict[str, Any], keys: Sequence[str], value: Any) -> bool:
    *parents, key = keys
    node = target
    for part in parents:
        node = node.setdefault(part, {})
    if node.get(key) == value:
        return False
    node[key] = value
    return True


def _channel(report: AgentReport, key: str) -> str:
    return next((c.state for c in report.channels if c.key == key), curb_reach.ABSENT)


def _claude(
    report: AgentReport,
    settings: ClaudeSettings,
    path: Path,
    home: Path,
    platform: str,
    backups_only: bool,
) -> tuple[Edit | None, list[str]]:
    try:
        before = curb_tighten.load(path)
    except ValueError:
        return None, [f"{LABELS[CLAUDE]} user settings cannot be read, so Curb changes nothing"]
    cwd = report.context.cwd
    after = copy.deepcopy(before)
    actions: list[str] = []
    guided: list[str] = []
    if not backups_only:
        actions, guided = _claude_channels(report, settings, after, home, cwd, platform)
        if not actions:
            return None, guided
    backups = _home_form(backups_dir(), home)
    rule = backups if backups.startswith("~") else "/" + posix(backups_dir())
    added = _append(after, "permissions.deny", [f"Read({rule}/**)", f"Edit({rule}/**)"])
    if platform != "win32":
        added += _append(after, "sandbox.filesystem.denyRead", [backups])
    if added:
        actions.append("deny Claude Code Curb's backup folder")
    if not actions:
        return None, guided
    text = json.dumps(after, indent=2) + "\n"
    return Edit(CLAUDE, path, before, after, text, tuple(actions)), guided


def _claude_channels(
    report: AgentReport,
    settings: ClaudeSettings,
    after: dict[str, Any],
    home: Path,
    cwd: Path,
    platform: str,
) -> tuple[list[str], list[str]]:
    actions: list[str] = []
    guided: list[str] = []
    by_read = [r for r in report.readable if curb_reach.FILE_TOOLS in r.via]
    by_shell = [r for r in report.readable if curb_reach.SHELL_FILES in r.via]
    rules = sorted(
        {
            _claude_rule(p, home, cwd)
            for r in by_read
            if r.credential.kind != "env"
            for p in r.credential.paths
        }
    )
    if any(r.credential.kind == "env" for r in by_read):
        rules.append("Read(//proc/self/environ)")
    added = _append(after, "permissions.deny", rules)
    if added:
        actions.append(f"add {added} Read deny rule(s)")

    network_open = _channel(report, curb_reach.SHELL_NETWORK) == curb_reach.UNCONTROLLED
    if (by_shell or network_open) and platform == "win32":
        guided.append(
            "Claude Code's sandbox does not run on native Windows: run Claude Code in WSL2 "
            "to close what shell commands can reach"
        )
    elif by_shell or network_open:
        turned = _set(after, ("sandbox", "enabled"), True)
        turned |= _set(after, ("sandbox", "allowUnsandboxedCommands"), False)
        if turned:
            actions.append(
                "turn on the sandbox with no way around it (commands that need other files "
                "or hosts will fail)"
            )
        files = sorted(
            {
                _sandbox_entry(p, home, cwd)
                for r in by_shell
                if r.credential.kind != "env"
                for p in r.credential.paths
            }
        )
        denied = _append(after, "sandbox.filesystem.denyRead", files)
        if denied:
            actions.append(f"deny the sandbox {denied} credential location(s)")
        names = sorted(
            {n for r in by_shell if r.credential.kind == "env" for n in r.credential.names}
        )
        masked = _append(
            after, "sandbox.credentials.envVars", [{"name": n, "mode": "deny"} for n in names]
        )
        if masked:
            actions.append(f"hide {masked} secret variable(s) from sandboxed commands")
        if network_open and _set(after, ("sandbox", "network", "strictAllowlist"), True):
            kept = len(settings.allowed_domains)
            actions.append(f"let sandboxed commands reach only the {kept} allowed domain(s)")
        if settings.excluded_commands:
            guided.append(
                "Commands in sandbox.excludedCommands run outside the sandbox: remove any you can"
            )
    if _channel(report, curb_reach.WEB) == curb_reach.UNCONTROLLED:
        guided.append(
            "Deny WebFetch and WebSearch in Claude Code, or deny WebFetch(domain:*) and allow "
            "only the domains you need"
        )
    return actions, guided


def _codex(
    report: AgentReport,
    settings: CodexSettings,
    path: Path,
    home: Path,
    backups_only: bool,
) -> tuple[Edit | None, list[str]]:
    try:
        before = curb_tighten.load(path)
        original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except (ValueError, OSError):
        return None, [f"{LABELS[CODEX]} config cannot be read, so Curb changes nothing"]
    after = copy.deepcopy(before)
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
    if not backups_only:
        _codex_channels(report, settings, before, profile, has_profile, home, change, guided)
        if not edits:
            return None, guided
    backups = _home_form(backups_dir(), home)
    if has_profile:
        change(
            ("permissions", profile, "filesystem", backups),
            "deny",
            "deny Codex Curb's backup folder",
        )
    else:
        guided.append(
            "Codex is not denied Curb's backup folder until it uses a permissions profile"
        )
    if not edits:
        return None, guided
    text = _toml_edit(original, edits)
    if text is None or _toml_parse(text) != after:
        guided.append(
            "Curb could not edit Codex's config.toml safely; make these changes by hand: "
            + "; ".join(dict.fromkeys(actions))
        )
        return None, guided
    return Edit(CODEX, path, before, after, text, tuple(dict.fromkeys(actions))), guided


def _codex_channels(
    report: AgentReport,
    settings: CodexSettings,
    before: Mapping[str, Any],
    profile: str,
    has_profile: bool,
    home: Path,
    change: Any,
    guided: list[str],
) -> None:
    if before.get("sandbox_mode") == "danger-full-access":
        change(("sandbox_mode",), "workspace-write", "run commands in the workspace-write sandbox")
    if settings.approval != "never":
        change(
            ("approval_policy",),
            "never",
            "stop asking to run commands outside the sandbox (such commands fail instead)",
        )
    if _channel(report, curb_reach.SHELL_NETWORK) == curb_reach.UNCONTROLLED:
        if settings.sandbox == "profile" and has_profile:
            if settings.network_access and not settings.network_domains:
                change(
                    ("permissions", profile, "network", "enabled"),
                    False,
                    "turn off network access for sandboxed commands",
                )
        elif settings.network_access and not settings.network_domains:
            change(
                ("sandbox_workspace_write", "network_access"),
                False,
                "turn off network access for sandboxed commands",
            )
    by_shell = [r for r in report.readable if curb_reach.SHELL_FILES in r.via]
    files = sorted(
        {
            _sandbox_entry(p, home, report.context.cwd)
            for r in by_shell
            if r.credential.kind not in ("env", "ssh-agent")
            for p in r.credential.paths
        }
    )
    if has_profile:
        for entry in files:
            change(
                ("permissions", profile, "filesystem", entry),
                "deny",
                "deny sandboxed commands a credential location",
            )
    elif files:
        guided.append(
            "Codex's older sandbox settings read every file: a permissions profile "
            "(default_permissions) can deny the credential folders"
        )
    names = sorted({n for r in by_shell if r.credential.kind == "env" for n in r.credential.names})
    policy = before.get("shell_environment_policy")
    held = policy.get("exclude") if isinstance(policy, Mapping) else None
    held = held if isinstance(held, list) else []
    new_names = [n for n in names if n not in held]
    if new_names:
        change(
            ("shell_environment_policy", "exclude"),
            [*held, *new_names],
            f"keep {len(new_names)} secret variable(s) from the commands Codex runs",
        )
    if _channel(report, curb_reach.WEB) == curb_reach.UNCONTROLLED:
        guided.append("Set Codex's web_search to cached or disabled")


# --- TOML, as text ---------------------------------------------------------------------------

_HEADER = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(#.*)?$")


def _toml_parse(text: str) -> dict[str, Any] | None:
    from .curb_settings import _toml_loads

    loads = _toml_loads()
    if loads is None:
        return None
    try:
        return loads(text)
    except ValueError:
        return None


def _toml_key(part: str) -> str:
    return part if re.fullmatch(r"[A-Za-z0-9_-]+", part) else json.dumps(part)


def _toml_value(value: Any) -> str:
    return json.dumps(value) if not isinstance(value, bool) else ("true" if value else "false")


def _toml_edit(text: str, edits: Sequence[tuple[tuple[str, ...], Any]]) -> str | None:
    """Set each key by editing lines, leaving the rest of the file as it was."""
    lines = text.splitlines()
    for keys, value in edits:
        table, key = keys[:-1], keys[-1]
        header = ".".join(_toml_key(p) for p in table)
        assignment = f"{_toml_key(key)} = {_toml_value(value)}"
        start, end = _section(lines, header)
        if start is None:  # only a table can be missing: the top level always exists
            lines += ["", f"[{header}]", assignment]
            continue
        pattern = re.compile(rf"^\s*{re.escape(_toml_key(key))}\s*=")
        hit = next((i for i in range(start, end) if pattern.match(lines[i])), None)
        if hit is not None:
            if lines[hit].rstrip().endswith("[") or "\\" in lines[hit]:
                return None  # a multi-line value: not safe to edit as one line
            lines[hit] = assignment
        else:
            last = end
            while last > start and not lines[last - 1].strip():
                last -= 1
            lines.insert(last, assignment)
    return "\n".join(lines) + "\n"


def _section(lines: list[str], header: str) -> tuple[int | None, int]:
    """The line range of a table's own keys: top level when header is empty."""
    if not header:
        end = next((i for i, line in enumerate(lines) if _HEADER.match(line)), len(lines))
        return 0, end
    for index, line in enumerate(lines):
        match = _HEADER.match(line)
        if match and match.group(1).strip() == header:
            end = next(
                (i for i in range(index + 1, len(lines)) if _HEADER.match(lines[i])), len(lines)
            )
            return index + 1, end
    return None, len(lines)


# --- applying and undoing -----------------------------------------------------------------


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _held(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _encoded(edit: Edit) -> bytes:
    """The bytes Curb writes: the new text, with the file's own line endings kept."""
    held = _held(edit.path)
    text = edit.text.replace("\n", "\r\n") if held and b"\r\n" in held else edit.text
    return text.encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.curb-tmp")
    temporary.write_bytes(data)
    if path.exists():
        with contextlib.suppress(OSError):
            shutil.copymode(path, temporary)
    os.replace(temporary, path)


def _private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def backup(edits: Sequence[Edit], *, now: float | None = None) -> Path:
    """Copy each file an edit will change, user-only, with what Curb is about to write."""
    created = now or time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(created))
    folder = backups_dir() / stamp
    suffix = 1
    while folder.exists():
        folder = backups_dir() / f"{stamp}-{suffix}"
        suffix += 1
    entries = []
    for number, edit in enumerate(edits):
        held = _held(edit.path)
        copy_name = f"{number}-{edit.path.name}"
        if held is not None:
            _private(folder / copy_name, held)
        entries.append(
            {
                "path": str(edit.path),
                "copy": copy_name if held is not None else None,
                "before": _sha(held),
                "written": _sha(_encoded(edit)),
            }
        )
    manifest = json.dumps({"created": created, "files": entries}, indent=1)
    _private(folder / "manifest.json", manifest.encode("utf-8"))
    return folder


def apply(plan: Plan, broker: Broker, grant: Grant | None) -> Path:
    """Write the plan's edits with a grant for exactly them. Returns the backup folder."""
    broker.redeem(grant, curb_approval.change_hash(plan.change()))
    prune()
    encoded = [_encoded(edit) for edit in plan.edits]
    folder = backup(plan.edits)
    try:
        for edit, data in zip(plan.edits, encoded, strict=True):
            _write_atomic(edit.path, data)
            if curb_tighten.load(edit.path) != edit.after:
                raise FixFailed(f"{edit.where} did not read back as written")
    except Exception:
        restore(folder, force=True)
        raise
    return folder


def latest() -> Path | None:
    folders = (
        sorted(p for p in backups_dir().glob("*") if (p / "manifest.json").is_file())
        if backups_dir().is_dir()
        else []
    )
    return folders[-1] if folders else None


def undo_change(folder: Path) -> dict[str, str]:
    """The change an undo would make, for the grant it needs."""
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    return {entry["path"]: entry["before"] or "" for entry in manifest["files"]}


def restore(folder: Path, *, force: bool = False) -> tuple[list[str], list[str]]:
    """Put backed-up files back. Without force, only files still holding what Curb wrote.

    Returns (restored, kept), each a list of file names.
    """
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    restored, kept = [], []
    for entry in manifest["files"]:
        path = Path(entry["path"])
        if not force and _sha(_held(path)) != entry["written"]:
            kept.append(path.name)  # edited since: putting the copy back would lose that
            continue
        if entry["copy"] is None:
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, (folder / entry["copy"]).read_bytes())
        restored.append(path.name)
    return restored, kept


def undo(broker: Broker, grant: Grant | None, folder: Path) -> tuple[list[str], list[str]]:
    """Restore a backup with a grant for exactly that undo."""
    broker.redeem(grant, curb_approval.change_hash(undo_change(folder)))
    return restore(folder)


def prune(*, now: float | None = None) -> None:
    """Drop backups older than seven days."""
    limit = (now or time.time()) - BACKUP_DAYS * 86400
    for folder in backups_dir().glob("*") if backups_dir().is_dir() else []:
        with contextlib.suppress(OSError, ValueError, KeyError):
            created = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["created"]
            if float(created) < limit:
                shutil.rmtree(folder)


def count() -> int:
    return sum(1 for _ in backups_dir().glob("*/manifest.json")) if backups_dir().is_dir() else 0


def forget_backups() -> None:
    """Delete every settings backup: the undo for each fix goes with them."""
    if backups_dir().is_dir():
        shutil.rmtree(backups_dir())
