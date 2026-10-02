"""Fleet reports: what each device tells its organization, signed and in order (Curb PRD §10.9).

A report holds only the minimised fields of §7.1: the device id, a
sequence number, when it was made, the agents and their versions, the
policy state, and counts by severity and exposure class. No paths,
usernames, hostnames, repo names or fingerprints.

Each report is signed with the device key and carries the hash of the
report before it, so the control plane refuses one whose sequence number
is not higher than the last, and an admin's `flanner curb fleet` can check
signatures and order itself, with the device keyring, without trusting
the console. Unsent reports wait in an outbox for 7 days.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import curb_store, curb_wire
from .curb_reach import AgentReport

OUTBOX_DAYS = 7
#: A device whose newest report is older than this shows as stale (§16).
STALE_HOURS = 24


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fields(
    *,
    device_id: str,
    organization_id: str,
    client_version: str,
    policy: Mapping[str, Any],
    reports: Sequence[AgentReport],
    exposure: Mapping[str, int] | None,
    checked_at: float,
) -> dict[str, Any]:
    """A report's fields, before it is numbered and signed."""
    agents = sorted(
        {
            (r.context.agent, r.version or "unknown")
            for r in reports
            if r.context.source == "default"
        }
    )
    severity = {"high": 0, "medium": 0, "low": 0}
    for report in reports:
        level = report.verdict.severity.lower()
        if level in severity:
            severity[level] += 1
    return {
        "kind": curb_wire.REPORT,
        "key_id": device_id,
        "device_id": device_id,
        "organization_id": organization_id,
        "client_version": client_version,
        "agents": [{"agent": a, "version": v} for a, v in agents],
        "policy": dict(policy),
        "severity": severity,
        "exposure": dict(exposure) if exposure is not None else None,
        "checked_at": _iso(checked_at),
    }


def issue(body: dict[str, Any], sign: Callable[[bytes], str], *, now: float | None = None) -> str:
    """Number, chain and sign a report, and queue it for sending. Returns the token."""
    stamp = now or time.time()
    state = curb_store.read_state("fleet")
    numbered = {
        **body,
        "sequence": int(state.get("sequence") or 0) + 1,
        "created_at": _iso(stamp),
        "previous_hash": str(state.get("previous_hash") or ""),
    }
    token = curb_wire.encode(numbered, sign)
    curb_store.write_state(
        "fleet",
        {"sequence": numbered["sequence"], "previous_hash": curb_wire.doc_hash(numbered)},
    )
    queued = [*_outbox(), {"token": token, "queued": stamp}]
    curb_store.write_state("fleet-outbox", {"reports": queued})
    return token


def _outbox() -> list[dict[str, Any]]:
    held = curb_store.read_state("fleet-outbox").get("reports")
    return (
        [r for r in held if isinstance(r, dict) and r.get("token")]
        if isinstance(held, list)
        else []
    )


def due(*, now: float | None = None) -> list[str]:
    """Queued reports, oldest first, after dropping any older than 7 days."""
    limit = (now or time.time()) - OUTBOX_DAYS * 86400
    kept = [r for r in _outbox() if float(r.get("queued") or 0) >= limit]
    curb_store.write_state("fleet-outbox", {"reports": kept})
    return [str(r["token"]) for r in kept]


def done(token: str) -> None:
    """Take a report out of the outbox: accepted, or refused for good."""
    kept = [r for r in _outbox() if r["token"] != token]
    curb_store.write_state("fleet-outbox", {"reports": kept})


# --- what an admin sees -----------------------------------------------------------------


@dataclass
class Device:
    device_id: str
    label: str
    latest: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)
    stale: bool = False

    @property
    def trusted(self) -> bool:
        return self.latest is not None and not self.problems


def _created(report: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(str(report["created_at"]).replace("Z", "+00:00"))


def verify(
    devices: Iterable[Mapping[str, Any]], keyring: Mapping[str, str], *, now: datetime
) -> list[Device]:
    """Check every device's reports with its own key: signatures, order, chain and age."""
    out = []
    for entry in devices:
        device_id = str(entry.get("device_id") or "")
        device = Device(device_id, str(entry.get("label") or device_id[:12]))
        key = keyring.get(device_id)
        held = entry.get("reports")
        tokens: list[Any] = held if isinstance(held, list) else []
        if not key:
            device.problems.append("no key for this device in the keyring")
            out.append(device)
            continue
        previous: dict[str, Any] | None = None
        for token in tokens:
            report = curb_wire.read(str(token), {device_id: key}, curb_wire.REPORT)
            if report is None or report.get("device_id") != device_id:
                device.problems.append("a report is not signed by this device")
                continue
            if previous is not None:
                if int(report["sequence"]) <= int(previous["sequence"]):
                    device.problems.append(
                        f"report {report['sequence']} is replayed or out of order"
                    )
                    continue
                if int(report["sequence"]) != int(previous["sequence"]) + 1:
                    device.stale = True  # a gap: a report never arrived
                elif report.get("previous_hash") != curb_wire.doc_hash(previous):
                    device.problems.append(f"report {report['sequence']} breaks the chain")
            previous = report
        device.latest = previous
        if previous is None or (now - _created(previous)).total_seconds() > STALE_HOURS * 3600:
            device.stale = True
        out.append(device)
    return out


def view(devices: Sequence[Device]) -> list[dict[str, Any]]:
    """Rows for `flanner curb fleet`: the newest trusted report per device."""
    rows = []
    for device in devices:
        latest = device.latest or {}
        policy = latest.get("policy") or {}
        rows.append(
            {
                "device": device.label,
                "device_id": device.device_id,
                "verified": device.trusted,
                "problems": device.problems,
                "stale": device.stale,
                "last_report": latest.get("created_at"),
                "agents": latest.get("agents") or [],
                "policy": policy,
                "severity": latest.get("severity"),
                "exposure": latest.get("exposure"),
            }
        )
    return rows


def matching(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How many devices share each compliance hash: one hash means one effective policy."""
    counts: dict[str, int] = {}
    for row in rows:
        value = (row.get("policy") or {}).get("compliance_hash")
        if value:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts
