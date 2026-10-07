"""Alerts: one for each change that grows what an agent can reach (Curb PRD §10.11).

Reach grows when an MCP server is added, a deny rule removed, a sandbox
turned off, or a new class A secret found (one sent to a model provider).
Curb keeps keyed digests of each, never names or values, and compares
them at every reconciliation: from Claude Code's ConfigChange hook, a
watch on Codex's config, the 6-hourly pass that also sees managed
settings, and each leak sweep. Policy refusals and integrity errors are
alerts too.

Each alert has a stable event id from the device id, the finding and the
change's sequence number (`curb_wire.event_id`), so a retry sends the same
id and the relay and webhook receivers drop repeats. A secret alerts once
per device, matched by its keyed digest. The developer is told at once,
by desktop notification and at their next session start; admins hear
through the control plane's relay when the organization has alerts.
The first pass only records what is there: nothing has changed yet.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from . import curb_store, curb_wire, notify
from .curb_context import LABELS
from .curb_reach import AgentReport
from .curb_settings import ClaudeSettings

MCP_ADDED = "mcp_server_added"
DENY_REMOVED = "deny_rule_removed"
SANDBOX_OFF = "sandbox_off"
SECRET_SENT = "secret_class_a"  # noqa: S105 - an alert type, not a secret
POLICY_REFUSED = "policy_refused"
POLICY_INTEGRITY = "policy_integrity"
POLICY_ROLLBACK = "policy_rollback"
AUTHORITY_REFUSED = "authority_refused"
REGISTRY_REFUSED = "registry_refused"

SAY = {
    MCP_ADDED: "an MCP server was added",
    DENY_REMOVED: "a deny rule was removed",
    SANDBOX_OFF: "the sandbox was turned off",
    SECRET_SENT: "a secret was sent to a model provider",
    POLICY_REFUSED: "an org policy was refused",
    POLICY_INTEGRITY: "an org policy failed its integrity check",
    POLICY_ROLLBACK: "an older org policy was refused",
    AUTHORITY_REFUSED: "a policy authority list was refused",
    REGISTRY_REFUSED: "an attribution registry was refused",
}
KEEP_DAYS = 7
SECRET_DAYS = 90
#: How long the developer's own list of alerts is kept, like the reports.
HISTORY_DAYS = 30


def _sandboxed(report: AgentReport) -> bool:
    settings = report.settings
    if isinstance(settings, ClaudeSettings):
        return settings.sandbox_enabled
    return settings.sandbox not in ("danger-full-access", "none", "unknown")


def snapshot(reports: Sequence[AgentReport], key: bytes) -> dict[str, Any]:
    """Keyed digests of each agent's MCP servers and deny rules, and its sandbox state."""
    agents: dict[str, Any] = {}
    for report in reports:
        if report.context.source != "default":
            continue
        settings = report.settings
        servers = [
            curb_store.digest(json.dumps([s.name, s.transport, list(s.command), s.url]), key)
            for s in settings.mcp
        ]
        if isinstance(settings, ClaudeSettings):
            rules = [r.text for r in settings.deny] + [
                f"sandbox:{p}" for p, _ in settings.deny_read
            ]
        else:
            rules = [f"deny_read:{p}" for p in settings.deny_read]
        agents[report.context.agent] = {
            "mcp": sorted(set(servers)),
            "deny": sorted({curb_store.digest(r, key) for r in rules}),
            "sandbox": _sandboxed(report),
        }
    return agents


def changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict[str, str]]:
    """What grew between two snapshots. An agent seen for the first time has no history."""
    found: list[dict[str, str]] = []
    for agent, now in new.items():
        before = old.get(agent)
        if not isinstance(before, Mapping):
            continue
        label = LABELS.get(agent, agent)
        for digest in sorted(set(now["mcp"]) - set(before.get("mcp") or [])):
            found.append(_finding(MCP_ADDED, agent, digest, "medium", f"{label} MCP settings"))
        for digest in sorted(set(before.get("deny") or []) - set(now["deny"])):
            found.append(_finding(DENY_REMOVED, agent, digest, "high", f"{label} deny rules"))
        if before.get("sandbox") and not now["sandbox"]:
            found.append(_finding(SANDBOX_OFF, agent, "", "high", f"{label} sandbox"))
    return found


def _finding(kind: str, agent: str, digest: str, severity: str, location: str) -> dict[str, str]:
    return {
        "type": kind,
        "agent": agent,
        "digest": digest,
        "severity": severity,
        "location": location,
    }


def secrets(sweep: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Class A findings from a stored sweep report, one per secret."""
    seen: dict[str, dict[str, str]] = {}
    for found in (sweep or {}).get("findings") or []:
        if found.get("class") == "A" and found.get("secret"):
            seen.setdefault(
                str(found["secret"]),
                _finding(SECRET_SENT, "", str(found["secret"]), "high", str(found["category"])),
            )
    return list(seen.values())


def policy(kind: str, version: Any, reason: str) -> dict[str, str]:
    return _finding(kind, "", "", "high", f"org policy {version or ''}".strip()) | {"why": reason}


def observe(
    reports: Sequence[AgentReport],
    key: bytes,
    *,
    sweep: Mapping[str, Any] | None = None,
    extra: Sequence[dict[str, str]] = (),
) -> list[dict[str, str]]:
    """Compare with what was last seen, keep the new picture, and return what grew."""
    state = curb_store.read_state("alerts")
    now = snapshot(reports, key)
    found = changes(state.get("agents") or {}, now) if state else []
    alerted = dict(state.get("secrets") or {})
    limit = time.time() - SECRET_DAYS * 86400
    alerted = {d: t for d, t in alerted.items() if float(t) >= limit}
    for item in secrets(sweep):
        if item["digest"] not in alerted:
            alerted[item["digest"]] = time.time()
            if state:
                found.append(item)
    curb_store.write_state(
        "alerts", {**state, "agents": now, "secrets": alerted, "seq": state.get("seq", 0)}
    )
    return [*found, *extra]


def raise_alerts(
    found: Sequence[dict[str, str]], *, device_id: str, now: float | None = None
) -> list[dict[str, Any]]:
    """Give one pass's findings their event ids, queue them, and tell the developer."""
    if not found:
        return []
    stamp = now or time.time()
    state = curb_store.read_state("alerts")
    sequence = int(state.get("seq") or 0) + 1
    curb_store.write_state("alerts", {**state, "seq": sequence})
    alerts = []
    for item in found:
        finding = ":".join((item["type"], item["agent"], item["digest"], item.get("location", "")))
        alerts.append(
            {
                "event_id": curb_wire.event_id(device_id, finding, sequence),
                "type": item["type"],
                "agent": item["agent"],
                "severity": item["severity"],
                "digest": item["digest"],
                "location": item["location"],
                "created_at": stamp,
            }
        )
    outbox = curb_store.read_state("alerts-outbox").get("alerts") or []
    queued = [*outbox, *({**a, "attempts": 0, "next": stamp} for a in alerts)]
    curb_store.write_state("alerts-outbox", {"alerts": queued})
    told = sorted({SAY[str(a["type"])] for a in alerts})
    text = "flanner curb: " + "; ".join(told) + ". Run `flanner curb policy` for what changed."
    notices = curb_store.read_state("alerts-notices").get("notices") or []
    curb_store.write_state("alerts-notices", {"notices": [*notices, text][-20:]})
    kept = [a for a in _held_history() if stamp - float(a["created_at"]) <= HISTORY_DAYS * 86400]
    curb_store.write_state("alerts-history", {"alerts": [*kept, *alerts]})
    notify.desktop("flanner curb: an agent can reach more", "; ".join(told).capitalize())
    return alerts


def _held_history() -> list[dict[str, Any]]:
    held = curb_store.read_state("alerts-history").get("alerts") or []
    return [a for a in held if isinstance(a, dict) and "created_at" in a]


def history(*, now: float | None = None) -> list[dict[str, Any]]:
    """The developer's own list: each alert of the last 30 days, newest first.

    `queued` says the alert still waits to reach the organization. Types,
    agents and redacted locations only, as the alerts themselves carry.
    """
    stamp = now or time.time()
    waiting = {
        a.get("event_id") for a in curb_store.read_state("alerts-outbox").get("alerts") or []
    }
    fresh = [
        {
            **a,
            "said": SAY.get(str(a.get("type")), str(a.get("type"))),
            "queued": a.get("event_id") in waiting,
        }
        for a in _held_history()
        if stamp - float(a["created_at"]) <= HISTORY_DAYS * 86400
    ]
    return sorted(fresh, key=lambda a: float(a["created_at"]), reverse=True)


def due(*, now: float | None = None) -> list[dict[str, Any]]:
    """Alerts to send now: retries keep their event ids; any older than 7 days are dropped."""
    stamp = now or time.time()
    held = curb_store.read_state("alerts-outbox").get("alerts") or []
    kept = [a for a in held if stamp - float(a.get("created_at") or 0) <= KEEP_DAYS * 86400]
    curb_store.write_state("alerts-outbox", {"alerts": kept})
    fields = ("event_id", "type", "agent", "severity", "digest", "location", "created_at")
    return [{k: a[k] for k in fields} for a in kept if float(a.get("next") or 0) <= stamp]


def settle(delivered: Sequence[str], failed: Sequence[str], *, now: float | None = None) -> None:
    """Drop delivered alerts; back off the failed ones, up to an hour apart."""
    stamp = now or time.time()
    held = curb_store.read_state("alerts-outbox").get("alerts") or []
    kept = []
    for alert in held:
        if alert["event_id"] in delivered:
            continue
        if alert["event_id"] in failed:
            attempts = int(alert.get("attempts") or 0) + 1
            alert = {**alert, "attempts": attempts, "next": stamp + min(3600, 30 * 2**attempts)}
        kept.append(alert)
    curb_store.write_state("alerts-outbox", {"alerts": kept})


def take_notices() -> list[str]:
    """The developer's unseen notices, once."""
    notices = curb_store.read_state("alerts-notices").get("notices") or []
    if notices:
        curb_store.write_state("alerts-notices", {"notices": []})
    return [str(n) for n in notices]
