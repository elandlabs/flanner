"""The action log: what each agent did, hash-chained and signed (Curb PRD §10.6).

One record format for Claude Code's hooks (PreToolUse, PostToolUse,
PermissionDenied) and Codex's (PreToolUse, PostToolUse), and for Curb's
own approvals. Metadata only: the agent, the session, the tool, the
channel it used, a redacted target (its kind and a keyed digest), the
program a shell command ran, the decision and the time. Never content.

Each record carries the hash of the one before it and its own hash,
signed with this device's key, so an edited, removed or reordered record
fails `verify`. Records older than 30 days are dropped; the chain then
restarts from a signed record naming the last one dropped.

A hook runs this on every tool call, so it imports little and fails
open: the agent keeps working if the log cannot be written.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import curb_store, identity, storage

LOG_DAYS = 30
GENESIS = "0" * 64
FILE_TOOLS, SHELL_FILES, SHELL_NETWORK, WEB, MCP = (
    "file_tools",
    "shell_files",
    "shell_network",
    "web",
    "mcp",
)
_CLAUDE_FILES = {"Read", "Glob", "Grep", "LS", "Edit", "MultiEdit", "Write", "NotebookEdit"}
_CLAUDE_SHELL = {"Bash", "PowerShell", "Monitor", "BashOutput", "KillShell"}
_CODEX_SHELL = {"shell", "local_shell", "exec_command", "container.exec", "Bash"}
#: Programs whose ordinary job is reaching the network.
_NETWORK = {
    "curl",
    "wget",
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "nc",
    "ncat",
    "telnet",
    "ftp",
    "http",
    "gh",
    "aws",
    "gcloud",
    "az",
    "kubectl",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "pip",
    "pip3",
    "uv",
    "docker",
    "Invoke-WebRequest",
    "Invoke-RestMethod",
    "iwr",
    "irm",
}
_GIT_NETWORK = {"push", "pull", "fetch", "clone", "ls-remote", "submodule"}
_SHELLS = {"bash", "sh", "zsh", "dash", "pwsh", "powershell", "bash.exe", "pwsh.exe"}
_DECISIONS = {"PreToolUse": "requested", "PostToolUse": "ran", "PermissionDenied": "denied"}


def log_path() -> Path:
    return curb_store.curb_dir() / "actions.jsonl"


def _canonical(entry: Mapping[str, Any]) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _seal(body: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    return {**body, "hash": digest, "sig": identity.sign(digest.encode("ascii"))}


def _last_hash() -> str:
    path = log_path()
    if not path.is_file():
        return GENESIS
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 65536))
        tail = handle.read().splitlines()
    for line in reversed(tail):
        if line.strip():
            try:
                return str(json.loads(line)["hash"])
            except (ValueError, KeyError):
                return "unreadable"
    return GENESIS


def append(entry: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Add one record to the chain, signed. Parallel hooks wait their turn."""
    with storage.exclusive_lock(curb_store.curb_dir(), "actions"):
        body = {**entry, "time": time.time() if now is None else now, "prev": _last_hash()}
        record = _seal(body)
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def records() -> list[dict[str, Any]]:
    """Every readable record, oldest first."""
    path = log_path()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def verify() -> tuple[bool, str]:
    """Whether every record is intact, in order, and signed by this device."""
    path = log_path()
    if not path.is_file():
        return True, "the log is empty"
    public = identity.device_public_key_b64()
    previous: str | None = None
    count = 0
    for count, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            record = json.loads(line)
        except ValueError:
            return False, f"record {count} cannot be read"
        digest, signature = record.pop("hash", ""), record.pop("sig", "")
        if hashlib.sha256(_canonical(record)).hexdigest() != digest:
            return False, f"record {count} was changed after it was written"
        if previous is None:
            if record.get("prev") != GENESIS and record.get("kind") != "start":
                return False, "records before the first one were removed"
        elif record.get("prev") != previous:
            return False, f"record {count} does not follow the one before it"
        if not identity.verify(public, digest.encode("ascii"), signature):
            return False, f"record {count} is not signed by this device"
        previous = digest
    return True, f"{count} record(s) intact and signed"


def prune(*, now: float | None = None) -> int:
    """Drop records older than 30 days. Returns how many went."""
    stamp = time.time() if now is None else now
    with storage.exclusive_lock(curb_store.curb_dir(), "actions"):
        held = records()
        keep = [r for r in held if stamp - float(r.get("time", 0)) <= LOG_DAYS * 86400]
        dropped = len(held) - len(keep)
        if not dropped:
            return 0
        last = held[dropped - 1]["hash"]
        chain = [_seal({"kind": "start", "dropped_through": last, "time": stamp, "prev": last})]
        for record in keep:
            body = {k: v for k, v in record.items() if k not in ("hash", "sig", "prev")}
            body["prev"] = chain[-1]["hash"]
            chain.append(_seal(body))
        log_path().write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in chain), encoding="utf-8"
        )
    return dropped


# --- hooks ----------------------------------------------------------------------------------


def channel_of(agent: str, tool: str, program: str | None, words: list[str]) -> str:
    """The channel a tool call used."""
    if tool.startswith("mcp__") or (agent == "codex" and tool.startswith("mcp")):
        return MCP
    if tool in ("WebFetch", "WebSearch", "web_search"):
        return WEB
    if tool in _CLAUDE_FILES or tool == "apply_patch":
        return FILE_TOOLS
    if tool in _CLAUDE_SHELL or tool in _CODEX_SHELL:
        network = program in _NETWORK or (
            program == "git" and len(words) > 1 and words[1] in _GIT_NETWORK
        )
        return SHELL_NETWORK if network else SHELL_FILES
    return "other"


def _target(given: Mapping[str, Any]) -> tuple[str, str, str | None, list[str]]:
    """The kind of target and its raw text, the program a command runs, and its words."""
    for key, kind in (
        ("file_path", "file"),
        ("path", "file"),
        ("notebook_path", "file"),
        ("url", "url"),
        ("query", "search"),
        ("pattern", "pattern"),
    ):
        value = given.get(key)
        if isinstance(value, str) and value:
            return kind, value, None, []
    command = given.get("command")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if isinstance(command, str) and command:
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        # Codex runs most commands as `bash -lc "<script>"`: read the script's own program.
        if len(words) > 2 and Path(words[0]).name in _SHELLS and words[1] in ("-c", "-lc"):
            try:
                words = shlex.split(words[2])
            except ValueError:
                words = words[2].split()
        program = Path(words[0]).name if words else None
        return "command", command, program, words
    return "", "", None, []


def from_hook(agent: str, raw: str, key: bytes) -> dict[str, Any] | None:
    """A record from one hook payload, or None when there is nothing to log."""
    payload = json.loads(raw) if raw.strip() else None
    if not isinstance(payload, dict):
        return None
    event = str(payload.get("hook_event_name") or payload.get("event") or "")
    tool = str(payload.get("tool_name") or payload.get("tool") or "")
    if not tool:
        return None
    given = payload.get("tool_input")
    kind, value, program, words = _target(given if isinstance(given, dict) else {})
    decision = _DECISIONS.get(event, event.lower() or "seen")
    response = payload.get("tool_response")
    if event == "PostToolUse" and isinstance(response, dict):
        if response.get("is_error") or response.get("error"):
            decision = "failed"
    return {
        "kind": "tool",
        "agent": agent,
        "session": str(payload.get("session_id") or ""),
        "event": event,
        "tool": tool,
        "channel": channel_of(agent, tool, program, words),
        "target": kind,
        "target_digest": curb_store.digest(value, key) if value else None,
        "program": program,
        "decision": decision,
    }


def record_hook(agent: str, raw: str) -> None:
    """The hook's whole job. Fails open: never stop the agent over a log line."""
    try:
        entry = from_hook(agent, raw, curb_store.digest_key())
        if entry is not None:
            append(entry)
    except Exception:  # noqa: BLE001 - a hook must never block the tool call it watches
        return


def record_approval(summary: str, decision: str) -> None:
    """Log an approval's outcome: granted, or refused (denied or ignored)."""
    try:
        append({"kind": "approval", "summary": summary, "decision": decision})
    except Exception:  # noqa: BLE001 - logging an outcome never changes the outcome
        return
