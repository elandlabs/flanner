"""Curb's team pass: check in, apply, reconcile, alert and report (Curb PRD §10.8-10.11).

One pass runs at session start, every 6 hours in the background receiver,
and on `flanner curb policy --check-in`:

1. fetch the policy authority list, and check in for the newest policy;
2. apply what only tightens under the delegation, and hold the rest;
3. re-read effective settings against the policy in force (drift);
4. raise one alert per change that grew what an agent can reach;
5. send the policy state, a signed fleet report, queued alerts and audit records.

Each network step runs only when the entitlement and the server both
offer its feature (`curb_wire.usable`). A failing step is recorded and the
pass carries on: the policy in force stays in force. The control plane is
reached only through `client`, which the composition root builds from
`account`, so nothing here can make a call on its own.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from . import (
    curb_alerts,
    curb_attribution,
    curb_export,
    curb_fleet,
    curb_policy,
    curb_store,
    curb_wire,
)
from .curb_reach import AgentReport
from .entitlements import CURB_ALERTS, CURB_ATTRIBUTION, CURB_FLEET, CURB_POLICY, Claims

#: How often the background receiver runs a pass, and the least time between two.
EVERY = 6 * 3600
QUIET = 300

_ALERT_FOR = {
    curb_policy.INTEGRITY: curb_alerts.POLICY_INTEGRITY,
    curb_policy.ROLLBACK: curb_alerts.POLICY_ROLLBACK,
    curb_policy.REJECTED: curb_alerts.POLICY_REFUSED,
}
_SAY = {
    curb_policy.APPLY: "a new org policy arrived",
    curb_policy.UNCHANGED: "the org policy is unchanged",
    curb_policy.NONE: "your organization has no Curb policy",
}


class Client(Protocol):
    """The control plane and the audit collector, as the composition root reaches them."""

    def call(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to the Curb endpoint `curb_wire.PATHS[name]`. Raises with a `code` on refusal."""
        ...

    def collect(self, url: str, token: str, records: list[dict[str, Any]]) -> None: ...


@dataclass(frozen=True)
class Device:
    device_id: str
    organization_id: str
    issuer_keyring: dict[str, str]
    claims: Claims | None
    offered: tuple[str, ...]
    sign: Callable[[bytes], str]
    version: str

    def offers(self, feature: str) -> bool:
        return curb_wire.usable(feature, self.claims, self.offered)


@dataclass
class Outcome:
    said: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    alerts: int = 0


def _code(error: Exception) -> str:
    return str(getattr(error, "code", "") or "")


def _check_in(client: Client, device: Device, now: datetime, out: Outcome) -> list[dict[str, str]]:
    raised: list[dict[str, str]] = []
    try:
        token = str(client.call("authority", {}).get("authority") or "")
        _, problem = curb_policy.accept_authority(token, device.issuer_keyring)
        if problem:
            out.problems.append(problem)
            raised.append(curb_alerts.policy(curb_alerts.AUTHORITY_REFUSED, None, problem))
    except Exception as e:  # noqa: BLE001 - one failed step must not stop the pass
        out.problems.append(f"the policy authority list could not be fetched: {e}")
    held = curb_policy.load().received
    try:
        answer = client.call(
            "policy",
            {
                "current_version": held.version if held else 0,
                "current_hash": held.hash if held else "",
            },
        )
        outcome, reason = curb_policy.receive(
            answer,
            issuer_keyring=device.issuer_keyring,
            organization_id=device.organization_id,
            now=now,
        )
    except Exception as e:  # noqa: BLE001
        out.problems.append(f"could not check in for the org policy: {e}")
        return raised
    if outcome in _ALERT_FOR:
        out.problems.append(reason)
        version = (curb_policy.load().rejected or {}).get("version")
        raised.append(curb_alerts.policy(_ALERT_FOR[outcome], version, reason))
    else:
        out.said.append(_SAY[outcome])
    return raised


def _apply(
    reports: Sequence[AgentReport], home: Path, env: Mapping[str, str], platform: str, out: Outcome
) -> bool:
    """The apply step. True when it wrote a settings file."""
    try:
        change = curb_policy.apply_received(reports, home=home, platform=platform, env=env)
    except Exception as e:  # noqa: BLE001
        out.problems.append(f"the org policy could not be applied: {e}")
        return False
    if change is None:
        return False
    state = curb_policy.load()
    if state.pending:
        out.said.append(
            "part of the org policy waits for your approval: run `flanner curb policy --approve`"
        )
    else:
        out.said.append(f"the org policy is applied ({(state.applied or {}).get('by')})")
    out.said += change.guided
    return bool(change.written)


def _attribution(client: Client, device: Device, out: Outcome) -> list[dict[str, str]]:
    """Register this device's new attribution keys, and refresh the registry."""
    for agent, entry in curb_attribution.keys()["keys"].items():
        if entry.get("registered"):
            continue
        try:
            client.call("attribution-keys", curb_attribution.registration(agent, device.device_id))
        except Exception as e:  # noqa: BLE001
            out.problems.append(f"an attribution key was not registered: {e}")
            continue
        curb_attribution.mark_registered(agent)
    try:
        token = str(client.call("attribution-registry", {}).get("registry") or "")
    except Exception as e:  # noqa: BLE001
        out.problems.append(f"the attribution registry could not be fetched: {e}")
        return []
    _, problem = curb_attribution.accept_registry(
        token, device.issuer_keyring, device.organization_id
    )
    if not problem:
        return []
    out.problems.append(problem)
    return [curb_alerts.policy(curb_alerts.REGISTRY_REFUSED, None, problem)]


def reconcile(
    reports: Sequence[AgentReport],
    device_id: str,
    *,
    home: Path,
    env: Mapping[str, str],
    platform: str,
    raised: Sequence[dict[str, str]] = (),
) -> list[dict[str, Any]]:
    """The local half of a pass: drift against the policy, and alerts for grown reach."""
    curb_policy.reconcile(reports, home=home, env=env, platform=platform)
    found = curb_alerts.observe(
        reports, curb_store.digest_key(), sweep=curb_store.latest_report("sweep"), extra=raised
    )
    return curb_alerts.raise_alerts(found, device_id=device_id)


def _send_alerts(client: Client, out: Outcome) -> None:
    """Send the alerts that are due, as many calls as the control plane's limit needs.

    Each call is settled from its own answer, so a refused batch holds no
    other back. Sent whole, a queue longer than the limit was refused on
    every pass and delivered nothing until it aged out.
    """
    waiting = curb_alerts.due()
    failed = ""
    for start in range(0, len(waiting), curb_wire.ALERTS_PER_CALL):
        batch = waiting[start : start + curb_wire.ALERTS_PER_CALL]
        ids = [a["event_id"] for a in batch]
        try:
            accepted = client.call("alerts", {"alerts": batch}).get("accepted") or []
        except Exception as e:  # noqa: BLE001
            curb_alerts.settle([], ids)
            failed = failed or str(e)
            continue
        curb_alerts.settle(
            [i for i in ids if i in accepted], [i for i in ids if i not in accepted]
        )
    if failed:
        out.problems.append(f"alerts are queued and will be retried: {failed}")


def _send_reports(client: Client, out: Outcome) -> None:
    from .refusals import STALE_SEQUENCE

    for token in curb_fleet.due():
        try:
            client.call("reports", {"report": token})
        except Exception as e:  # noqa: BLE001
            if _code(e) == STALE_SEQUENCE:
                curb_fleet.done(token)  # refused for good: never accepted later
                continue
            out.problems.append(f"fleet reports are queued and will be retried: {e}")
            return
        curb_fleet.done(token)


def _export(client: Client, device: Device, env: Mapping[str, str], out: Outcome) -> None:
    policy = curb_policy.load().received
    target = policy.export if policy else None
    token = curb_store.read_secret("export")
    if not target or not token:
        return
    url = str(target["url"])
    records, dropped = curb_export.pending()
    if dropped:
        out.problems.append(f"{dropped} audit record(s) were dropped: the backlog was too long")
    try:
        if records:
            events = [
                curb_export.ocsf(r, device_id=device.device_id, version=device.version)
                for r in records
            ]
            client.collect(url, token, events)
            curb_export.sent(records)
        folder = env.get(curb_export.LOG_DIR_ENV)
        if target.get("openshell") is True and folder:
            passed, offsets = curb_export.openshell(Path(folder), curb_store.digest_key())
            if passed:
                client.collect(url, token, passed)
            curb_export.openshell_sent(offsets)
    except Exception as e:  # noqa: BLE001
        out.problems.append(f"audit records wait in the action log: {e}")


def cycle(
    client: Client,
    device: Device,
    assess: Callable[[], Sequence[AgentReport]],
    *,
    home: Path,
    env: Mapping[str, str] | None = None,
    platform: str,
    now: datetime | None = None,
) -> Outcome:
    """One full pass. `assess` returns fresh reports; it runs again after a write."""
    out = Outcome()
    moment = now or datetime.now(timezone.utc)
    environment = dict(os.environ) if env is None else dict(env)
    reports = assess()
    raised: list[dict[str, str]] = []
    if device.offers(CURB_POLICY):
        raised = _check_in(client, device, moment, out)
        if _apply(reports, home, environment, platform, out):
            reports = assess()
    else:
        # Says so, as the PRD asks: a check-in that did nothing and said
        # nothing read as a control plane with no policy to give.
        why = curb_wire.unusable(CURB_POLICY, device.claims, device.offered)
        out.said.append(f"{why}, so nothing was checked in and nothing changes")
    if device.offers(CURB_ATTRIBUTION):
        raised += _attribution(client, device, out)
    alerts = reconcile(
        reports, device.device_id, home=home, env=environment, platform=platform, raised=raised
    )
    out.alerts = len(alerts)
    state = curb_policy.load()
    listing = curb_policy.authority(device.issuer_keyring)
    summary = curb_policy.summary(state, curb_policy.flags(state, listing, moment))
    if device.offers(CURB_ALERTS):
        _send_alerts(client, out)
    if device.offers(CURB_POLICY):
        try:
            client.call("state", summary)
        except Exception as e:  # noqa: BLE001
            out.problems.append(f"the policy state was not sent: {e}")
        _export(client, device, environment, out)
    if device.offers(CURB_FLEET):
        sweep = curb_store.latest_report("sweep")
        body = curb_fleet.fields(
            device_id=device.device_id,
            organization_id=device.organization_id,
            client_version=device.version,
            policy=summary,
            reports=reports,
            exposure=(sweep or {}).get("by_class"),
            checked_at=time.time(),
        )
        curb_fleet.issue(body, device.sign)
        _send_reports(client, out)
    curb_store.write_state("team", {"last": time.time()})
    return out


def due(*, now: float | None = None, every: float = EVERY) -> bool:
    """Whether a pass is due: none yet, or the last one is older than `every`."""
    last = float(curb_store.read_state("team").get("last") or 0)
    return not last or (now or time.time()) - last >= every
