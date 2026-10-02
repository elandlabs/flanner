"""Audit export: the action log as OCSF, for the organization's collector (Curb PRD §10.10).

Each tool call in Curb's action log becomes one OCSF 1.9.0 API Activity
record (class 6003) with the AI Operation profile: the agent, its local
session id, the tool, the decision, the redacted target (its kind and a
keyed digest) and the time. Never content and never a path. The signed
policy names the collector; its token comes with the check-in and is kept
in the OS keychain (`curb_store.keep_secret`).

The action log is the buffer. Records not yet sent stay in it; a backlog
over 10,000 records is skipped and counted as dropped.

NVIDIA OpenShell writes its own OCSF records as JSON lines. When the policy
asks for them and `FLANNER_OPENSHELL_LOG_DIR` names where they land, they
are passed through too, with command lines, paths, queries and bodies
replaced by keyed digests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import curb_log, curb_store
from .curb_context import LABELS

OCSF_VERSION = "1.9.0"
API_ACTIVITY, APPLICATION = 6003, 6
MAX_BACKLOG = 10_000
BATCH = 500
LOG_DIR_ENV = "FLANNER_OPENSHELL_LOG_DIR"

_READS = {"Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "web_search"}
_WRITES = {"Edit", "MultiEdit", "Write", "NotebookEdit", "apply_patch"}
_ACTIVITY = {2: "Read", 3: "Update", 99: "Invoke"}
#: decision -> (disposition_id, disposition, status_id)
_DECISION = {
    "requested": (0, "Unknown", 0),
    "ran": (1, "Allowed", 1),
    "failed": (1, "Allowed", 2),
    "denied": (2, "Blocked", 0),
}
#: OpenShell fields that can carry a secret or a path.
_REDACT = {"cmd_line", "path", "query", "body", "value", "url_string"}


def ocsf(record: Mapping[str, Any], *, device_id: str, version: str) -> dict[str, Any]:
    """One action-log record as an OCSF API Activity event."""
    tool = str(record.get("tool") or "")
    agent = LABELS.get(str(record.get("agent")), str(record.get("agent")))
    session = str(record.get("session") or "")
    activity = 2 if tool in _READS else 3 if tool in _WRITES else 99
    disposition, disposition_name, status = _DECISION.get(
        str(record.get("decision")), (99, "Other", 0)
    )
    event: dict[str, Any] = {
        "class_uid": API_ACTIVITY,
        "class_name": "API Activity",
        "category_uid": APPLICATION,
        "category_name": "Application Activity",
        "activity_id": activity,
        "activity_name": _ACTIVITY[activity],
        "type_uid": API_ACTIVITY * 100 + activity,
        "severity_id": 1,
        "severity": "Informational",
        "time": int(float(record.get("time") or 0) * 1000),
        "metadata": {
            "version": OCSF_VERSION,
            "product": {"name": "flanner curb", "vendor_name": "flanner", "version": version},
            "log_name": "curb.actions",
            "uid": str(record.get("hash") or ""),
            "profiles": ["ai_operation"],
        },
        "actor": {"application": {"name": agent}, "session": {"uid": session}},
        "api": {"operation": tool, "service": {"name": agent}},
        "src_endpoint": {"uid": device_id},
        "cloud": {"provider": "local"},
        "ai_agent": {"name": agent, "instance_uid": session},
        "disposition_id": disposition,
        "disposition": disposition_name,
        "status_id": status,
        "unmapped": {"channel": record.get("channel"), "event": record.get("event")},
    }
    if record.get("target_digest"):
        event["resources"] = [{"type": record.get("target"), "uid": record["target_digest"]}]
    return event


def pending(*, limit: int = BATCH) -> tuple[list[dict[str, Any]], int]:
    """The next tool records to send, and how many a too-long backlog dropped."""
    state = curb_store.read_state("export")
    cursor = state.get("cursor")
    held = [r for r in curb_log.records() if r.get("kind") == "tool"]
    hashes = [r.get("hash") for r in held]
    start = hashes.index(cursor) + 1 if cursor in hashes else 0
    backlog = held[start:]
    dropped = max(0, len(backlog) - MAX_BACKLOG)
    if dropped:
        skipped = backlog[dropped - 1]
        curb_store.write_state(
            "export",
            {
                **state,
                "cursor": skipped["hash"],
                "dropped": int(state.get("dropped", 0)) + dropped,
            },
        )
        backlog = backlog[dropped:]
    return backlog[:limit], dropped


def sent(records: Sequence[Mapping[str, Any]]) -> None:
    """Move the cursor past records the collector accepted."""
    if records:
        state = curb_store.read_state("export")
        curb_store.write_state("export", {**state, "cursor": records[-1]["hash"]})


def _redacted(value: Any, key: bytes) -> Any:
    if isinstance(value, dict):
        return {
            k: (
                "digest:" + curb_store.digest(v, key)
                if k in _REDACT and isinstance(v, str)
                else _redacted(v, key)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redacted(v, key) for v in value]
    return value


def openshell(
    folder: Path, key: bytes, *, limit: int = BATCH
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """OpenShell's OCSF records not yet sent, redacted, and the new read offsets."""
    offsets = dict(curb_store.read_state("export").get("openshell") or {})
    out: list[dict[str, Any]] = []
    for path in sorted(folder.glob("openshell-ocsf.*.log")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        start = int(offsets.get(path.name, 0))
        for number, line in enumerate(lines[start:], start=start + 1):
            if len(out) >= limit:
                return out, offsets
            try:
                event = json.loads(line)
            except ValueError:
                offsets[path.name] = number
                continue
            if isinstance(event, dict):
                out.append(_redacted(event, key))
            offsets[path.name] = number
    return out, offsets


def openshell_sent(offsets: Mapping[str, int]) -> None:
    state = curb_store.read_state("export")
    curb_store.write_state("export", {**state, "openshell": dict(offsets)})
